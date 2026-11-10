"""Regression tests for the Phase-3 adversarial review (DECISIONS D-033).

Each test fails on the code the review saw (5d0de86). The license-gate
cases live in tests/test_license_gate.py.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


# --- Perses reads the config the image is started with ----------------------


def test_perses_config_is_mounted_where_the_image_reads_it():
    svc = yaml.safe_load(read("deploy", "compose.yaml"))["services"]["perses"]
    targets = {v.split(":")[1] for v in svc["volumes"]}
    # persesdev/perses:v0.55.1 CMD: --config=/etc/perses/config.yaml
    config_flag = next((a for a in svc.get("command", []) if a.startswith("--config=")), None)
    expected = config_flag.split("=", 1)[1] if config_flag else "/etc/perses/config.yaml"
    assert expected in targets, targets
    provisioned = yaml.safe_load(read("deploy", "observability", "perses", "config.yaml"))
    for folder in provisioned["provisioning"]["folders"]:
        assert any(t.startswith(folder + "/") for t in targets), folder


# --- Headroom savings gauge can reach the proxy from an agent container -----


def test_agents_know_where_the_headroom_stats_are():
    env = yaml.safe_load(read("deploy", "compose.yaml"))["services"]["agent-backend"]["environment"]
    assert env["HEADROOM_STATS_URL"] == "${HEADROOM_STATS_URL-http://headroom:8787}"
    for manifest in ("agent-backend.yaml", "agent-roles.yaml"):
        docs = [d for d in yaml.safe_load_all(read("deploy", "k8s", manifest)) if d]
        deployments = [d for d in docs if d["kind"] == "Deployment"]
        assert deployments
        for d in deployments:
            for c in d["spec"]["template"]["spec"]["containers"]:
                names = {e["name"]: e.get("value") for e in c.get("env", [])}
                assert names.get("HEADROOM_STATS_URL") == "http://headroom:8787", d["metadata"]


def _stats_url(**env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("HEADROOM_")}
    out = subprocess.check_output(
        [sys.executable, "-c", "from agent import sla_metrics as m; print(m.HEADROOM_STATS_URL)"],
        cwd=ROOT,
        env={**clean, **env},
    )
    return out.decode().strip()


def test_headroom_stats_url_follows_the_proxy_url():
    assert _stats_url() == "http://localhost:8787"
    assert _stats_url(HEADROOM_URL="http://headroom:8787/v1") == "http://headroom:8787"
    assert (
        _stats_url(HEADROOM_URL="http://h:1/v1/", HEADROOM_STATS_URL="http://s:2") == "http://s:2"
    )


# --- --reembed never wipes vectors when the model is unavailable ------------


async def test_reembed_refuses_without_a_model_and_keeps_embeddings(pg, monkeypatch):
    import seed_knowledge as seed

    vec = "[" + ",".join(["0.05"] * 384) + "]"
    await pg.execute(
        "INSERT INTO lessons (lesson_id, text, tag, embedding) "
        "VALUES (gen_random_uuid(), 'keep me', 'test', $1::vector)",
        vec,
    )
    monkeypatch.setattr(seed, "embed", lambda text: None)  # lessons.py text-only degradation
    with pytest.raises(SystemExit) as err:
        await seed.reembed(pg)
    assert "refused" in str(err.value)
    assert await pg.fetchval("SELECT count(*) FROM lessons WHERE embedding IS NOT NULL") == 1

    monkeypatch.setattr(seed, "embed", lambda text: [0.1] * 384)
    assert await seed.reembed(pg) == 1
    assert await pg.fetchval("SELECT count(*) FROM lessons WHERE embedding IS NOT NULL") == 1


# --- the relay's optional /metrics listener cannot stop publishing ----------


def test_relay_metrics_listener_handles_a_busy_port():
    src = read("relay", "relay.ts")
    server = src[src.index("createServer(") : src.index(".listen(METRICS_PORT)")]
    assert '.on("error"' in server


# --- verify-integration leaves eval/last_report.md behind -------------------


async def test_report_mode_writes_last_report(tmp_path, monkeypatch, capsys):
    import shadow_eval

    golden = tmp_path / "eval" / "golden"
    golden.mkdir(parents=True)
    monkeypatch.setattr(shadow_eval, "GOLDEN_DIR", str(golden))
    run = {"green_rate": 0.5, "avg_tokens": 10, "avg_latency_s": 1.0, "tasks": 2, "results": []}
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({**run, "label": "a"}))
    b.write_text(json.dumps({**run, "label": "b"}))
    args = argparse.Namespace(report=[str(a), str(b)], require_not_worse=True)
    assert await shadow_eval.main_async(args) == 0
    report = (tmp_path / "eval" / "last_report.md").read_text()
    assert report.startswith("# Shadow-eval report")
    assert report.strip() == capsys.readouterr().out.strip()


# --- `make migrate` no longer applies the optional bge-m3 switch ------------


def _migrations():
    return sorted(glob.glob(os.path.join(ROOT, "db", "migrations", "*.sql")))


def test_blanket_migrations_never_resize_the_embedding_column():
    names = [os.path.basename(p) for p in _migrations()]
    assert names and not any("bge_m3" in n for n in names), names
    for path in _migrations():
        with open(path) as f:
            assert not re.search(r"ALTER\s+COLUMN\s+embedding\s+TYPE", f.read(), re.I), path
    assert os.path.exists(
        os.path.join(ROOT, "db", "migrations", "optional", "001_bge_m3_embeddings.sql")
    )
    recipe = read("Makefile").split("\nmigrate:")[1].split("\n\n")[0]
    assert "db/migrations/*.sql" in recipe and "ON_ERROR_STOP=1" in recipe and "|| exit 1" in recipe


async def test_migrating_twice_keeps_384_dim_embeddings(pg):
    vec = "[" + ",".join(["0.05"] * 384) + "]"
    await pg.execute(
        "INSERT INTO lessons (lesson_id, text, tag, embedding) "
        "VALUES (gen_random_uuid(), 'keep me', 'test', $1::vector)",
        vec,
    )
    for _ in range(2):  # the upgrade path, then a second run: idempotent, no error
        for path in _migrations():
            with open(path) as f:
                await pg.execute(f.read())
    assert await pg.fetchval("SELECT count(*) FROM lessons WHERE embedding IS NOT NULL") == 1
    dim = await pg.fetchval(
        "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'lessons'::regclass "
        "AND attname = 'embedding'"
    )
    assert dim == 384


# --- the lite smoke keeps the bot token off every command line --------------


def test_lite_smoke_token_is_not_passed_in_argv():
    src = read("scripts", "lite_smoke.sh")
    for line in src.splitlines():
        code = line.split("#", 1)[0]
        if "$LITE_SMOKE_FORGEJO_TOKEN" in code or "${LITE_SMOKE_FORGEJO_TOKEN}" in code:
            pytest.fail(f"token expanded on a command line: {line}")
    assert "env.LITE_SMOKE_FORGEJO_TOKEN" in src and "export LITE_SMOKE_FORGEJO_TOKEN" in src


def test_jq_reads_the_token_from_the_environment():
    if shutil.which("jq") is None:
        pytest.skip("jq not installed")
    src = read("scripts", "lite_smoke.sh")
    jq_line = next(line for line in src.splitlines() if line.startswith("jq -n"))
    program = re.search(r"'([^']+)'", jq_line).group(1)
    out = subprocess.check_output(
        ["jq", "-n", program], env={**os.environ, "LITE_SMOKE_FORGEJO_TOKEN": "synthetic-token"}
    )
    assert json.loads(out) == {"data": {"token": "synthetic-token"}}


# --- torch-cpu-lock restores every file it regenerates ----------------------

STUB_UV = """#!/usr/bin/env bash
# stub: `lock` succeeds, every `export` still resolves CUDA wheels
case "$1" in
  lock) exit 0 ;;
  export) printf 'torch==2.9.0\\nnvidia-cudnn-cu12==9.1\\n' ;;
esac
"""


def test_torch_cpu_lock_failure_leaves_the_tree_as_it_found_it(tmp_path):
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    tracked = ["pyproject.toml", "uv.lock"] + sorted(
        os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "requirements*.txt"))
    )
    for name in tracked:
        shutil.copy(os.path.join(ROOT, name), work / name)
    for script in ("torch_cpu_lock.sh", "lock_export.sh"):
        shutil.copy(os.path.join(ROOT, "scripts", script), work / "scripts" / script)
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "uv").write_text(STUB_UV)
    (stub / "uv").chmod(0o755)
    before = {name: (work / name).read_bytes() for name in tracked}

    proc = subprocess.run(
        ["bash", "scripts/torch_cpu_lock.sh"],
        cwd=work,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "CUDA wheels still resolved" in proc.stdout
    changed = [name for name in tracked if (work / name).read_bytes() != before[name]]
    assert changed == [], changed


# --- the diff-size gate counts authored files next to generated ones --------


def _generated_list():
    src = read(".forgejo", "scripts", "diff_size_gate.sh")
    return src.split("GENERATED=(")[1].split(")")[0].split()


def test_diff_size_gate_excludes_exactly_the_generated_files():
    requirements = [os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "requirements*.txt"))]
    emitted = set(re.findall(r"emit (requirements[\w-]*\.txt)", read("scripts", "lock_export.sh")))
    emitted |= {f"requirements-{x}.txt" for x in ("vector", "orchestration", "routing", "ccompat")}
    assert set(requirements) == emitted  # every requirements file is one `make lock` writes
    protoc = [
        os.path.relpath(p, ROOT)
        for p in glob.glob(os.path.join(ROOT, "agent", "gen", "**", "*_pb2.py*"), recursive=True)
    ]
    assert sorted(_generated_list()) == sorted(["uv.lock", *requirements, *protoc])
    src = read(".forgejo", "scripts", "diff_size_gate.sh")
    assert "requirements*.txt'" not in src and "agent/gen/**" not in src  # no patterns


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _gate_repo(tmp_path, files):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "integration", str(work)], check=True)
    (work / "requirements.txt").write_text("a==1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "integration")
    _git(work, "checkout", "-q", "-b", "feature/x")
    for rel, lines in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"line {i}\n" for i in range(lines)))
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "change")
    return subprocess.run(
        ["bash", os.path.join(ROOT, ".forgejo", "scripts", "diff_size_gate.sh")],
        cwd=work,
        env={**os.environ, "BASE_BRANCH": "integration"},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "path",
    [
        "agent/gen/handwritten_logic.py",
        "requirements-notes.txt",
        "requirements-x/deep/authored.txt",
    ],
)
def test_authored_file_beside_generated_ones_is_counted(tmp_path, path):
    proc = _gate_repo(tmp_path, {path: 2000})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "2000 changed line(s)" in proc.stdout


def test_regenerated_files_are_still_not_counted(tmp_path):
    generated = {"requirements.txt": 3000, "uv.lock": 3000, "agent/gen/swarm/v1/task_pb2.py": 3000}
    proc = _gate_repo(tmp_path, {**generated, "agent/worker.py": 10})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "10 changed line(s)" in proc.stdout


# --- the agent image carries the tools its quality gate runs ----------------


def test_agent_image_installs_the_gate_tools():
    pins = read("requirements.txt")
    assert re.search(r"^ruff==", pins, re.M) and re.search(r"^pytest==", pins, re.M)
    sync = next(
        line for line in read("deploy", "Containerfile.agent").splitlines() if "uv sync" in line
    )
    assert "--group agent" in sync and "--group gate" in sync and "--no-dev" in sync


def test_missing_gate_tools_are_reported(monkeypatch):
    from agent import sandbox

    monkeypatch.setattr(sandbox.shutil, "which", lambda tool: None)
    assert sandbox.missing_gate_tools() == ["pytest", "ruff"]
    monkeypatch.setattr(sandbox.shutil, "which", lambda tool: f"/opt/venv/bin/{tool}")
    assert sandbox.missing_gate_tools() == []
    monkeypatch.setattr(sandbox, "QUALITY_FAST_CHECK", "")
    monkeypatch.setattr(sandbox, "QUALITY_CHECKS", "mytool --strict;")
    monkeypatch.setattr(sandbox.shutil, "which", lambda tool: None)
    assert sandbox.missing_gate_tools() == ["mytool"]
