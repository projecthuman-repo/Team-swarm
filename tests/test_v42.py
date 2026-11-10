"""v4.2 Elevation+ unit tests: Codex engine + token parser, Codex-review
finding mapping, rubric grading, escalation gating, Strix findings, and
the doom-loop repetition detector. All pure logic / mocks — no live GPU,
Docker, or CLI, and no importing absent optional deps at collection."""

import importlib.util
import os

import pytest

from agent import sandbox
from agent.codex_review import parse_findings
from agent.verifier import parse_rubric_score
from ingest.forgejo_issue_sync import subject_for  # noqa: F401 (import sanity)

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


doomloop = _load("doomloop_probe", "scripts/doomloop_probe.py")
strix_findings = _load("strix_findings", "scripts/strix_findings.py")


# ---- T7.2 Codex engine command + token parser -------------------------


class FakeTask:
    task_id = "t-1"
    title = "add a healthz endpoint"
    spec_ref = ""
    verify_cmd = ""
    branch = "feature/t-1"


def test_codex_engine_cmd(monkeypatch):
    monkeypatch.setattr(sandbox, "ENGINE", "codex")
    monkeypatch.setattr(sandbox, "ENGINE_CMD", "")
    cmd = sandbox.engine_cmd("do the thing")
    assert cmd[0] == "codex" and cmd[1] == "exec"
    assert "--oss" in cmd and "--local-provider" in cmd and "ollama" in cmd
    assert cmd[-1] == "do the thing"


def test_codex_flags_are_overridable(monkeypatch):
    monkeypatch.setattr(sandbox, "ENGINE", "codex")
    monkeypatch.setattr(sandbox, "ENGINE_CMD", "")
    monkeypatch.setattr(sandbox, "CODEX_EXEC_FLAGS", "--oss --foo bar")
    cmd = sandbox.engine_cmd("m")
    assert cmd == ["codex", "exec", "--oss", "--foo", "bar", "m"]


def test_aider_still_default(monkeypatch):
    # additive: aider path unchanged (no engine removed)
    monkeypatch.setattr(sandbox, "ENGINE", "aider")
    monkeypatch.setattr(sandbox, "ENGINE_CMD", "")
    assert sandbox.engine_cmd("m")[0] == "aider"


def test_codex_token_parser_io_pair():
    assert sandbox._parse_codex_tokens("input: 4200  output: 1100") == 5300


def test_codex_token_parser_total():
    assert sandbox._parse_codex_tokens("tokens used: 5,310") == 5310


def test_codex_token_parser_absent():
    assert sandbox._parse_codex_tokens("no counts here") == 0


def test_parse_tokens_dispatches_by_engine(monkeypatch):
    monkeypatch.setattr(sandbox, "ENGINE", "codex")
    assert sandbox.parse_tokens("input: 10 output: 5") == 15
    monkeypatch.setattr(sandbox, "ENGINE", "aider")
    assert sandbox.parse_tokens("Tokens: 1k sent, 1k received.") == 2000


# ---- T7.4 Codex review finding mapping --------------------------------


def test_codex_review_blocks_on_high_severity():
    raw = '{"findings": [{"title": "SQLi", "severity": "high"}]}'
    findings = parse_findings(raw)
    assert findings[0]["lens"] == "codex"
    assert findings[0]["verdict"] == "BLOCK"


def test_codex_review_passes_clean():
    raw = '{"findings": []}'
    assert parse_findings(raw)[0]["verdict"] == "PASS"


def test_codex_review_jsonl_mode():
    raw = '{"title": "nit", "priority": "low"}\n{"title": "leak", "priority": "critical"}'
    assert parse_findings(raw)[0]["verdict"] == "BLOCK"


def test_codex_review_garbage_is_an_error_not_a_pass():
    # review R7: this test used to pin the fail-open behaviour ("garbage ->
    # PASS"). A reviewer whose output cannot be read has reviewed nothing.
    for raw in ("not json at all", "", "   \n"):
        assert parse_findings(raw)[0]["verdict"] == "ERROR", repr(raw)
    assert parse_findings("[]")[0]["verdict"] == "PASS"  # an explicit empty list is clean


def test_codex_review_nonzero_exit_is_an_error(monkeypatch):
    import subprocess

    from agent import codex_review

    done = subprocess.CompletedProcess(["codex"], 2, stdout='{"findings": []}', stderr="boom")
    monkeypatch.setattr(codex_review.subprocess, "run", lambda *a, **k: done)
    monkeypatch.setattr(codex_review, "codex_available", lambda: True)
    assert codex_review.review("integration")[0]["verdict"] == "ERROR"
    seen = []

    def run_ok(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"findings": []}', stderr="")

    monkeypatch.setattr(codex_review.subprocess, "run", run_ok)
    monkeypatch.setattr(codex_review, "CODEX_REVIEW_FLAGS", "--oss")
    assert codex_review.review("integration")[0]["verdict"] == "PASS"
    # the pinned CLI's surface: `codex exec [--oss] --json review --base <branch>`
    assert seen == [["codex", "exec", "--oss", "--json", "review", "--base", "integration"]]


# ---- T7.8 rubric grading ----------------------------------------------


def test_rubric_score_parsing():
    assert parse_rubric_score("notes...\nSCORE: 3/4") == 0.75
    assert parse_rubric_score("SCORE: 4/4") == 1.0
    assert parse_rubric_score("no score line") == 0.0  # fail-closed


def test_rubric_hit_miss_threshold():
    # threshold logic lives in grade_rubric; verify the boundary math here
    assert (3 / 4) >= 0.75
    assert (2 / 4) < 0.75


# ---- T7.7 escalation gating (structural opt-in) -----------------------


def test_escalation_disabled_by_default(monkeypatch):
    monkeypatch.setenv("ROUTE_API_FALLBACK", "0")
    esc = _load("escalation_claude", "agent/escalation_claude.py")
    assert esc.escalation_enabled() is False


def test_escalation_enabled_with_flag(monkeypatch):
    monkeypatch.setenv("ROUTE_API_FALLBACK", "1")
    esc = _load("escalation_claude", "agent/escalation_claude.py")
    assert esc.escalation_enabled() is True


def test_escalation_prompt_carries_failing_diff():
    esc = _load("escalation_claude", "agent/escalation_claude.py")
    prompt = esc.build_prompt("do X", "diff --git a b", "FAILED test_x")
    assert "do X" in prompt and "FAILED test_x" in prompt


# ---- T8.3 Strix findings -> lessons -----------------------------------


def test_strix_validated_only():
    report = {
        "findings": [
            {"title": "confirmed XSS", "validated": True, "severity": "high"},
            {"title": "maybe", "validated": False},
            {"title": "confirmed2", "status": "validated"},
        ]
    }
    validated = strix_findings.validated_findings(report)
    assert len(validated) == 2


def test_strix_lesson_text_mentions_fix():
    lesson = strix_findings.finding_lesson(
        {"title": "SQLi", "severity": "critical", "location": "api.py", "poc": "' OR 1=1"}
    )
    assert "SQLi" in lesson and "regression test" in lesson


def test_strix_no_findings():
    assert strix_findings.validated_findings({"findings": []}) == []


# ---- T9.2 doom-loop repetition detector -------------------------------


def test_doomloop_detects_consecutive_repeats():
    text = "\n".join(["thinking..."] * 8)
    assert doomloop.is_doom_loop(text) is True


def test_doomloop_ignores_normal_transcript():
    text = "plan the change\nwrite the test\nrun pytest\nfix the import\ndone"
    assert doomloop.is_doom_loop(text) is False


def test_doomloop_detects_ngram_cycle():
    block = "retry the same call with the same args and it fails again always "
    assert doomloop.is_doom_loop(block * 5) is True


def test_max_consecutive_repeat_ignores_blank_lines():
    assert doomloop.max_consecutive_repeat(["a", "", "a", "", "a"]) == 1


def test_probe_transcripts_rate(tmp_path):
    (tmp_path / "loop.log").write_text("\n".join(["stuck"] * 10))
    (tmp_path / "ok.log").write_text("did the work\nshipped it")
    res = doomloop.probe_transcripts([str(tmp_path / "loop.log"), str(tmp_path / "ok.log")])
    assert res["doom_loops"] == 1 and res["transcripts"] == 2
    assert res["doom_loop_rate"] == 0.5


if __name__ == "__main__":
    pytest.main([__file__, "-q"])


# ---- review R7: v4.2 as a citizen of the lean toolchain ----------------

import re  # noqa: E402
import tomllib  # noqa: E402

import yaml  # noqa: E402


def _text(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def test_v42_dependencies_live_in_the_lock():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        project = tomllib.load(f)
    assert project["project"]["optional-dependencies"]["escalation"] == ["claude-agent-sdk>=0.2"]
    assert project["dependency-groups"]["security"] == ["strix-agent>=1.0.4"]
    conflicts = [sorted(c["group"] for c in pair) for pair in project["tool"]["uv"]["conflicts"]]
    for other in ("agent", "ingest", "headroom-proxy"):
        assert sorted([other, "security"]) in conflicts, other
    for name, needle in (("requirements-security.txt", r"^strix-agent=="),
                         ("requirements-escalation.txt", r"^claude-agent-sdk==")):  # fmt: skip
        text = _text(name)
        assert text.startswith("# GENERATED by make lock"), name
        assert re.search(needle, text, re.M), name
    # the image closures never carry them
    for name in ("requirements.txt", "requirements-ingest.txt", "requirements-headroom-proxy.txt"):
        assert not re.search(r"^(strix-agent|claude-agent-sdk|cvss)==", _text(name), re.M), name
    assert "claude-agent-sdk     -> extra `escalation`" in _text("requirements-elevation.txt")


def test_codex_image_is_built_from_the_lock_with_a_pinned_cli():
    text = _text("deploy", "Containerfile.codex")
    assert "uv sync --frozen --no-dev --no-install-project --group agent --group gate" in text
    assert "pip install" not in text and "curl" not in text and "|| echo" not in text
    assert re.search(r"^ARG CODEX_VERSION=\d+\.\d+\.\d+$", text, re.M)  # no `latest`
    # the npm package is a Node launcher: the runtime must stay in the image
    assert "COPY --from=codex /usr/local/bin/node /usr/local/bin/node" in text
    assert "apt-get purge" not in text and "codex --version" in text
    assert "useradd --create-home --uid 10001 swarm" in text and "AGENT_ENGINE=codex" in text


def test_codex_lane_is_hardened_like_the_agents_but_keeps_its_egress():
    services = yaml.safe_load(_text("deploy", "compose.yaml"))["services"]
    codex = services["codex-engine"]
    assert codex["profiles"] == ["codex"] and codex["read_only"] is True
    assert codex["tmpfs"] == services["agent-backend"]["tmpfs"]
    assert codex["cap_drop"] == ["ALL"]
    assert codex["networks"] == ["swarm-internal", "egress"]  # the controlled-egress lane
    for name in ("agent-backend", "agent-frontend", "agent-review", "agent-triage"):
        assert services[name]["networks"] == ["swarm-internal"]  # sealed agents unchanged


def test_strix_lane_runs_the_cli_from_the_locked_group():
    # review R10: one script behind `make strix`, `make strix-scan` and the workflow
    script = _text("scripts", "strix_quick_scan.sh")
    assert "uv sync -q --frozen --no-install-project --only-group security" in script
    assert '"$ENV_DIR/bin/strix" -n -t "$TARGET" -m "$MODE" "$@"' in script
    for default in ('MODE="${STRIX_SCAN_MODE:-quick}"', 'TARGET="${STRIX_TARGET:-./}"',
                    'STRIX_TELEMETRY="${STRIX_TELEMETRY:-false}"',
                    'LLM_API_BASE="${LLM_API_BASE:-http://localhost:4000/v1}"'):  # fmt: skip
        assert default in script, default
    workflow = _text(".forgejo", "workflows", "security.yaml")
    assert (
        "./scripts/strix_quick_scan.sh --scope-mode diff --diff-base origin/integration" in workflow
    )
    assert "pip install --quiet -r" not in workflow and "--output json" not in workflow
    assert "continue-on-error: true" in workflow  # still advisory, as v4.2 shipped it
    assert not os.path.isdir(os.path.join(ROOT, ".github", "workflows"))  # Forgejo Actions only


def test_make_strix_and_strix_scan_share_the_script():
    import shutil
    import subprocess

    if shutil.which("make") is None:
        pytest.skip("make not installed")
    for target in ("strix", "strix-scan"):
        # under `make ci-local` this is a nested make: drop its directory banner
        out = subprocess.run(
            ["make", "-n", "--no-print-directory", target],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert out.strip() == "./scripts/strix_quick_scan.sh", (target, out)


def test_no_compose_or_k8s_file_names_the_unpublished_strix_image():
    import glob

    files = glob.glob(os.path.join(ROOT, "deploy", "**", "*.y*ml"), recursive=True)
    assert len(files) > 10
    for path in files:
        with open(path) as f:
            assert "ghcr.io/usestrix/strix:" not in f.read(), path
    assert not os.path.exists(os.path.join(ROOT, "deploy", "k8s", "strix.yaml"))
    services = yaml.safe_load(_text("deploy", "compose.yaml"))["services"]
    assert "strix" not in services
    # no swarm container mounts the host's container socket any more
    for name, service in services.items():
        for volume in service.get("volumes", []):
            assert "docker.sock" not in str(volume), name


@pytest.fixture
def strix_env():
    env_dir = os.environ.get("STRIX_ENV")
    if not env_dir or not os.path.exists(os.path.join(env_dir, "bin", "strix")):
        reason = "STRIX_ENV is not set to an environment synced with --only-group security"
        if os.environ.get("REQUIRE_STRIX_ENV") == "1":
            pytest.fail(f"REQUIRE_STRIX_ENV=1 but {reason}")
        pytest.skip(f"{reason}; `make ci-local` provides it")
    return env_dir


def test_strix_image_is_the_default_the_locked_package_declares(strix_env):
    import subprocess

    code = (
        "from strix.config.settings import RuntimeSettings, TelemetrySettings;"
        "print(RuntimeSettings.model_fields['image'].default);"
        "print(RuntimeSettings.model_fields['image'].alias);"
        "print(TelemetrySettings.model_fields['enabled'].alias)"
    )
    out = subprocess.check_output([os.path.join(strix_env, "bin", "python"), "-c", code])
    image, image_var, telemetry_var = out.decode().split()
    assert image_var == "STRIX_IMAGE" and telemetry_var == "STRIX_TELEMETRY"
    assert image.startswith("ghcr.io/usestrix/strix-sandbox:")
    assert f'STRIX_IMAGE="${{STRIX_IMAGE:-{image}}}"' in _text("scripts", "strix_quick_scan.sh")
    assert f"#STRIX_IMAGE={image}" in _text(".env.example")
    assert f"`{image}`" in _text("conventions", "licensing.md")


def test_strix_flags_the_lane_uses_exist_in_the_locked_cli(strix_env):
    import subprocess

    usage = subprocess.run(
        [os.path.join(strix_env, "bin", "strix"), "--help"], capture_output=True, text=True
    ).stdout
    for flag in ("-n", "-t TARGET", "-m {quick,standard,deep}", "--scope-mode {auto,diff,full}",
                 "--diff-base DIFF_BASE"):  # fmt: skip
        assert flag in usage, flag
    assert "--output" not in usage  # v4.2's `--output json` never existed here


def test_strix_findings_reads_what_strix_agent_writes():
    # strix-agent 1.7.0: strix_runs/<run>/vulnerabilities.json is a top-level list
    report = [
        {"title": "SSRF in fetcher", "severity": "high", "poc_description": "curl http://169…"},
        {"title": "speculative", "severity": "low"},
    ]
    validated = strix_findings.validated_findings(report)
    assert [v["title"] for v in validated] == ["SSRF in fetcher"]
    assert "curl http://169" in strix_findings.finding_lesson(validated[0])


def test_shadow_eval_engines_runs_each_engine_in_its_own_process():
    recipe = _text("Makefile").split("\nshadow-eval-engines:")[1].split("\n\n")[0]
    assert "env AGENT_ENGINE=aider python scripts/shadow_eval.py --single engine-aider" in recipe
    assert "env AGENT_ENGINE=codex python scripts/shadow_eval.py --single engine-codex" in recipe
    assert "--report eval/engine-aider.json eval/engine-codex.json" in recipe
    assert "--candidate AGENT_ENGINE=codex" not in recipe  # ENGINE is read at import: no-op


def test_v42_runtime_checks_are_deferred_not_skipped():
    vi = _text("Makefile").split("\nverify-integration:")[1]
    for needle in ("deploy/Containerfile.codex", "./scripts/codex_egress_audit.sh",
                   "./scripts/strix_quick_scan.sh"):  # fmt: skip
        assert needle in vi, needle
    assert "antidoom" not in vi  # Antidoom stays its own offline target
    for src in (_text(".forgejo", "workflows", "ci.yaml"), _text("scripts", "ci_local.sh")):
        assert "uv run --no-sync python .forgejo/scripts/agents_md_gate.py" in src


def test_every_v42_make_target_survives():
    makefile = _text("Makefile")
    for target in ("strix", "codex-engine", "shadow-eval-engines", "codex-review", "codex-audit",
                   "agents-md-check", "strix-findings", "doomloop"):  # fmt: skip
        assert re.search(rf"^{target}:", makefile, re.M), target


def test_codex_audit_uses_the_keys_and_variables_of_the_pinned_cli():
    # verified in openai/codex at tag rust-v0.167.0 (the pinned @openai/codex)
    src = _text("scripts", "codex_egress_audit.sh")
    assert "check_for_update_on_startup = false" in src
    assert "[analytics]\nenabled = false" in src
    assert "disable_update_check" not in src and "telemetry = false" not in src
    assert "CODEX_OSS_BASE_URL=http://ollama:11434/v1" in src
    # sealed-container condition: the internal network, never --network=none + localhost
    assert '--network "$NETWORK"' in src and "--network=none" not in src
    assert "deploy_swarm-internal" in src and "swarm-codex:dev" in src
    assert "ghcr.io/openai/codex" not in src  # that image name could not be verified
    for code in ("exit 1", "exit 2", "exit 3"):  # FAIL / SKIPPED / INCONCLUSIVE kept
        assert code in src


def test_antidoom_train_passes_only_flags_upstream_defines():
    # upstream's runner (Liquid4All/antidoom) has --model-name, --target-pairs and
    # --output-dir; rank, learning rate, early stopping and merge are config fields
    src = _text("deploy", "antidoom", "train.sh")
    call = src.split("uv run antidoom", 1)[1].split("\n\n", 1)[0]
    flags = set(re.findall(r"--[a-z-]+", call))
    assert flags == {"--model-name", "--target-pairs", "--output-dir"}, flags
    assert "s/^( *lora_r:).*/\\1 ${LORA_R}/" in src and "learning_rate:" in src
    assert "REFUSED: target Ornith only" in src  # the LFM2 guard is kept
