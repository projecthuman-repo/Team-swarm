"""L1.6 — sandbox workspace policy: capped tmpfs, workdir root, orphan reaper."""

import os
import time

import yaml

from agent import sandbox

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def make_dir(root, name, age_s, now):
    path = root / name
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "f.py").write_text("x = 1\n")
    os.utime(path, (now - age_s, now - age_s))
    return path


def test_reaper_removes_aged_orphan_and_keeps_fresh(tmp_path):
    now = time.time()
    aged = make_dir(tmp_path, "task-deadbeef-abc", 1801, now)
    fresh = make_dir(tmp_path, "task-cafebabe-def", 10, now)
    other = make_dir(tmp_path, "not-a-task", 99999, now)
    removed = sandbox.reap_orphans(str(tmp_path), max_age_s=1800, now=now)
    assert removed == [str(aged)]
    assert not aged.exists()
    assert fresh.exists() and other.exists()


def test_reaper_never_follows_symlinks(tmp_path):
    now = time.time()
    target = make_dir(tmp_path / "elsewhere", "keep-me", 99999, now)
    link = tmp_path / "task-link"
    link.symlink_to(target)
    os.utime(link, (now - 99999, now - 99999), follow_symlinks=False)
    assert sandbox.reap_orphans(str(tmp_path), max_age_s=1, now=now) == []
    assert (target / "repo" / "f.py").exists()


def test_reaper_defaults_and_missing_root(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "SANDBOX_WORKDIR", str(tmp_path))
    monkeypatch.setattr(sandbox, "TASK_TIMEOUT_S", 60)
    now = time.time()
    old = make_dir(tmp_path, "task-0001-x", 61, now)
    assert sandbox.reap_orphans() == [str(old)]
    assert sandbox.reap_orphans(str(tmp_path / "absent")) == []


def test_heartbeat_keeps_a_live_workdir_fresh(tmp_path):
    now = time.time()
    live = make_dir(tmp_path, "task-live-1", 5000, now)
    sandbox._heartbeat(str(live))
    assert sandbox.reap_orphans(str(tmp_path), max_age_s=1800) == []


def test_worker_reaps_on_boot():
    src = open(os.path.join(ROOT, "agent", "agent_worker.py")).read()
    main = src[src.index("async def main()") :]
    assert main.index("reap_orphans()") < main.index("asyncpg.create_pool")


def test_tmpfs_caps():
    with open(os.path.join(ROOT, "deploy", "compose.yaml")) as f:
        compose = yaml.safe_load(f)
    tmpfs = compose["services"]["agent-backend"]["tmpfs"]
    assert tmpfs == ["/tmp:size=${SANDBOX_TMPFS_SIZE:-4g}", "/home/swarm:size=1g"]
    for name in ("agent-backend.yaml", "agent-roles.yaml"):
        with open(os.path.join(ROOT, "deploy", "k8s", name)) as f:
            docs = [d for d in yaml.safe_load_all(f) if d and d.get("kind") == "Deployment"]
        assert docs
        for d in docs:
            vols = {v["name"]: v["emptyDir"] for v in d["spec"]["template"]["spec"]["volumes"]
                    if "emptyDir" in v}  # fmt: skip
            assert vols["tmp"]["sizeLimit"] == "4Gi"
            assert vols["home"]["sizeLimit"] == "1Gi"
