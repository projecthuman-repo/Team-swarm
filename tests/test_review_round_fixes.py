"""Fixes for findings of the adversarial review of R1–R7 that have no better
home: one test per confirmed finding (the license gate, the Strix lane, the
Headroom upstream and the Codex lane have theirs in their own test files)."""

import os
import re

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def text(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def test_no_verify_integration_step_label_can_run_a_command():
    # a backtick inside the double-quoted label ran `codex --version` on the host
    recipe = text("Makefile").split("\nverify-integration:")[1].split("\n\n")[0]
    steps = [line for line in recipe.splitlines() if line.startswith('\t@$(VI) "')]
    assert len(steps) >= 15
    for line in steps:
        label = re.match(r'\t@\$\(VI\) "([^"]*)"', line)
        assert label, line
        assert "`" not in label.group(1) and "$(" not in label.group(1), line


def test_antidoom_runs_in_the_clone_and_does_not_need_pip_in_the_image():
    service = yaml.safe_load(text("deploy", "antidoom", "compose.yaml"))["services"]["antidoom"]
    # `uv run antidoom -c configs/default.yaml` needs the project: the clone itself
    assert service["working_dir"] == "/work/antidoom"
    assert "./antidoom-src:/work/antidoom" in service["volumes"]
    assert "pip install" not in service["command"]
    assert service["command"].startswith("uv run antidoom -c configs/default.yaml")
    # the host's uv, read-only
    assert "${UV_BIN:-/usr/local/bin/uv}:/usr/local/bin/uv:ro" in service["volumes"]
    readme = text("deploy", "antidoom", "README.md")
    assert "UV_BIN=$(command -v uv) docker compose -f deploy/antidoom/compose.yaml up" in readme


def test_the_tier_b_embedding_model_pull_has_a_procedure_that_works_under_default_deny():
    deny = yaml.safe_load(text("deploy", "k8s", "networkpolicy-default-deny.yaml"))
    assert deny["spec"]["podSelector"] == {}  # every pod, the embeddings pod included
    pull = yaml.safe_load(text("deploy", "k8s", "embeddings-model-pull.yaml"))
    assert pull["kind"] == "NetworkPolicy" and pull["metadata"]["namespace"] == "swarm"
    assert pull["spec"]["podSelector"] == {"matchLabels": {"app": "embeddings"}}  # that pod alone
    assert pull["spec"]["policyTypes"] == ["Egress"]
    (rule,) = pull["spec"]["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]
    # no agent, relay or other workload carries the label the policy selects
    for name in os.listdir(os.path.join(ROOT, "deploy", "k8s")):
        if name.endswith(".yaml") and not name.startswith("embeddings"):
            assert "app: embeddings" not in text("deploy", "k8s", name), name
    # temporary: never applied by the installer, and the header says to delete it
    assert "embeddings-model-pull.yaml" not in [
        line.split("#")[0] for line in text("deploy", "k8s", "install.sh").splitlines()
    ] and "kubectl apply -f embeddings-model-pull.yaml" not in text("deploy", "k8s", "install.sh")
    header = text("deploy", "k8s", "embeddings.yaml").split("apiVersion:")[0]
    steps = [
        "kubectl apply  -f deploy/k8s/embeddings-model-pull.yaml",
        "kubectl exec -n swarm deploy/embeddings -- ollama pull all-minilm",
        "kubectl delete -f deploy/k8s/embeddings-model-pull.yaml",
    ]
    assert [header.index(step) for step in steps] == sorted(header.index(s) for s in steps)
    assert "kubectl delete -f deploy/k8s/embeddings-model-pull.yaml" in text("docs", "RUNBOOK.md")


def test_version_labels_follow_the_release():
    assert 'description = "Overnight Agent Swarm v4.2.0 ' in text("pyproject.toml")
    assert '"name": "Swarm SLA — v4.2.0"' in text("deploy", "observability", "perses-sla.json")


def test_licensing_md_describes_the_gate_as_it_is():
    doc = text("conventions", "licensing.md")
    assert "syft over\n`uv.lock`" not in doc  # Python is judged from publisher metadata since R2
    assert "see License overrides" not in doc  # py-rust-stemmers has no row
    assert "for the agent, ingest, headroom and codex images" in doc
    assert "(agent, ingest, relay, headroom, codex)" in doc
    for row in ("| fastembed |", "| ↳ mmh3 / loguru |", "| ↳ py-rust-stemmers |"):
        line = next(ln for ln in doc.splitlines() if ln.startswith(row))
        assert "| B — in no image |" in line, row
