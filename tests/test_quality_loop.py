"""v4.1 T2.1/T2.2/T2.6 — green gate, grounded repair, repro-test policy;
Lean & Precise L2.1 — the two-stage gate."""

import os
import subprocess

import pytest

from agent import sandbox
from agent.sandbox import RunResult, build_message, repair_message


class FakeTask:
    task_id = "t-1"
    title = "Bug: pagination off by one"
    spec_ref = ""
    branch = "feature/t-1"
    verify_cmd = ""
    budget_tokens = 1000
    deadline_unix = 0


def test_repair_message_carries_actual_tool_output():
    msg = repair_message("fix the bug", "FAILED tests/test_x.py::test_y", 1)
    assert "FAILED tests/test_x.py::test_y" in msg
    assert "REPAIR CYCLE 1" in msg
    # external signal only — never ask the model to re-check itself
    assert "re-check yourself" not in msg.lower()


def test_repair_message_truncates_long_output():
    msg = repair_message("fix", "x" * 10_000, 2)
    assert len(msg) < 6_000


def test_build_message_includes_repro_test_policy():
    msg = build_message(FakeTask())
    # conventions/repro-tests.md is appended when the policy is on
    assert "reproduction test" in msg.lower()


def test_build_message_includes_verify_cmd():
    task = FakeTask()
    task.verify_cmd = "pytest tests/test_pagination.py -q"
    msg = build_message(task)
    assert "pytest tests/test_pagination.py -q" in msg


def test_run_result_defaults():
    r = RunResult()
    assert r.first_pass_green is True
    assert r.repair_cycles == 0


def test_max_repair_cycles_default():
    assert sandbox.MAX_REPAIR_CYCLES == 2


# --- Lean & Precise L2.1 — two-stage gate -----------------------------------


def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")  # fmt: skip
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "README.md").write_text("synthetic fixture\n")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, env=env, check=True)
    return repo, env


@pytest.fixture
def recorded(monkeypatch):
    calls = []
    real = sandbox._run

    async def spy(cmd, cwd, env, timeout, check=True):
        calls.append(list(cmd))
        return await real(cmd, cwd, env, timeout, check)

    monkeypatch.setattr(sandbox, "_run", spy)
    monkeypatch.setattr(sandbox, "QUALITY_CHECKS", "ruff check .;pytest -q")
    monkeypatch.setattr(sandbox, "QUALITY_FAST_CHECK", "ruff check")
    return calls


async def test_changed_python_files_sees_modified_and_untracked(tmp_path):
    repo, env = git_repo(tmp_path)
    (repo / "app.py").write_text("def add(a, b):\n    return b + a\n")
    (repo / "new_mod.py").write_text("X = 1\n")
    (repo / "notes.txt").write_text("not python\n")
    files = await sandbox.changed_python_files(str(repo), env)
    assert sorted(files) == ["app.py", "new_mod.py"]


async def test_syntax_error_repairs_from_stage1_without_pytest(tmp_path, recorded):
    repo, env = git_repo(tmp_path)
    (repo / "app.py").write_text("def add(a, b)\n    return a + b\n")  # missing colon
    timings = {}
    green, out = await sandbox.check_workspace(str(repo), env, "pytest -q", timings)
    assert green is False
    assert ["ruff", "check", "app.py"] in recorded
    assert not any(c[0] == "pytest" for c in recorded), recorded
    assert out.startswith("$ ruff check app.py")
    assert "pytest" not in out
    prompt = repair_message("fix add()", out, 1)
    assert "app.py" in prompt and "REPAIR CYCLE 1" in prompt
    assert isinstance(timings["stage1_ms"], int) and timings["stage2_ms"] is None


async def test_green_change_runs_both_stages(tmp_path, recorded):
    repo, env = git_repo(tmp_path)
    (repo / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    timings = {}
    green, out = await sandbox.check_workspace(str(repo), env, "", timings)
    assert green is True, out
    assert ["ruff", "check", "test_app.py"] in recorded
    assert ["ruff", "check", "."] in recorded and ["pytest", "-q"] in recorded
    assert isinstance(timings["stage1_ms"], int) and timings["stage1_ms"] >= 0
    assert isinstance(timings["stage2_ms"], int) and timings["stage2_ms"] >= 0


async def test_no_python_change_skips_stage1(tmp_path, recorded):
    repo, env = git_repo(tmp_path)
    (repo / "README.md").write_text("synthetic fixture, edited\n")
    timings = {}
    green, _ = await sandbox.check_workspace(str(repo), env, "", timings)
    assert green is True
    assert not any(c[:2] == ["ruff", "check"] and c[2:] != ["."] for c in recorded)
    assert timings["stage1_ms"] is None and isinstance(timings["stage2_ms"], int)


async def test_verify_cmd_runs_in_stage2(tmp_path, recorded):
    repo, env = git_repo(tmp_path)
    green, out = await sandbox.check_workspace(str(repo), env, "false", {})
    assert green is False
    assert ["false"] in recorded and "$ false" in out


async def test_committed_engine_edit_still_hits_stage1(tmp_path, recorded):
    """Phase-2 review fix: aider auto-commits; stage 1 diffs against the base."""
    repo, env = git_repo(tmp_path)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()  # fmt: skip
    (repo / "app.py").write_text("def add(a, b)\n    return a + b\n")
    (repo / "new_mod.py").write_text("X = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "aider: edit"], cwd=repo, env=env, check=True)
    assert await sandbox.changed_python_files(str(repo), env) == []  # what HEAD would see
    timings = {}
    green, out = await sandbox.check_workspace(str(repo), env, "", timings, base)
    assert green is False and out.startswith("$ ruff check app.py new_mod.py")
    assert not any(c[0] == "pytest" for c in recorded)
    assert isinstance(timings["stage1_ms"], int)


async def test_fast_check_default_respects_ruff_excludes(tmp_path, monkeypatch):
    assert sandbox.QUALITY_FAST_CHECK == "ruff check --force-exclude"
    repo, env = git_repo(tmp_path)
    (repo / "pyproject.toml").write_text('[tool.ruff]\nextend-exclude = ["gen"]\n')
    (repo / "gen").mkdir()
    (repo / "gen" / "pb2.py").write_text("import os\n")  # F401 — but excluded
    monkeypatch.setattr(sandbox, "QUALITY_CHECKS", "ruff check .")
    green, out = await sandbox.check_workspace(str(repo), env, "", {})
    assert green is True, out
