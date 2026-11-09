"""L3.3 — the golden pack: ten tasks, two per role, deterministic verify commands."""
# ruff: noqa: E501 — the workspace fixtures below are long literal file contents

import glob
import importlib.util
import json
import os
import shlex
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PACK = {
    os.path.basename(p): json.load(open(p))
    for p in sorted(glob.glob(os.path.join(ROOT, "eval", "golden", "*.json")))
}
spec = importlib.util.spec_from_file_location(
    "shadow_eval_l33", os.path.join(ROOT, "scripts", "shadow_eval.py")
)
shadow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shadow)

LIMITER = """
import threading


class RateLimiter:
    def __init__(self, rate, capacity):
        self.rate, self.capacity = rate, capacity
        self.tokens, self.last, self.lock = float(capacity), None, threading.Lock()

    def allow(self, now):
        with self.lock:
            if self.last is not None:
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False
"""
TASK_MD = (
    "role: {role}\ntitle: {title}\nverify_cmd: pytest -q tests/test_x.py\n\n## Scope\nOne thing.\n"
)

# name -> (files of a correct workspace, files of a wrong one)
FIXTURES = {
    "004_frontend_status_page.json": (
        {"web/index.html": "<html><head><title>Swarm status</title></head><body><main><h1>Swarm</h1>"
         '<table id="tasks"><tr><th>Task</th><th>Role</th><th>Status</th></tr></table></main></body></html>'},
        {"web/index.html": "<html><head><title>Swarm status</title></head><body><main><h1>a</h1><h1>b</h1>"
         '<table id="tasks"><tr><th>Task</th><th>Status</th></tr></table></main></body></html>'},
    ),
    "005_frontend_no_inline_styles.json": (
        {"web/dashboard.html": '<link rel="stylesheet" href="styles.css"><div class="card wide">'
         '<p class="muted">x</p></div>',
         "web/styles.css": ".card { padding: 1rem }\n.wide { width: 100% }\n.muted { color: gray }\n"},
        {"web/dashboard.html": '<link rel="stylesheet" href="styles.css"><div class="card wide" '
         'style="color:red"><p class="muted">x</p></div>',
         "web/styles.css": ".card {}\n.wide {}\n.muted {}\n"},
    ),
    "006_review_sql_injection.json": (
        {"REVIEW.md": "app/users.py:14: high: SQL injection — `name` is interpolated; use a bound parameter\n"},
        {"REVIEW.md": "app/users.py:14: low: consider a shorter variable name\n"},
    ),
    "007_review_swallowed_error.json": (
        {"REVIEW.md": "app/cache.py:8: high: the broad except swallows the error and leaves `data` unbound\n"
         "app/cache.py:4: medium: mutable default argument `seen=[]` is shared across calls\n"},
        {"REVIEW.md": "app/cache.py:8: high: the broad except swallows the error\n"},
    ),
    "008_triage_vague_slowness.json": (
        {"triage/slow-app.md": "role: backend\ntitle: Profile and fix slow task list endpoint\n"
         "verify_cmd: pytest -q tests/test_perf.py\n\n## Reproduction\nLoad /tasks with 10k rows.\n\n"
         "## Scope\nThe list query.\n\n## Out of scope\nFrontend rendering.\n"},
        {"triage/slow-app.md": "role: backend\ntitle: Profile and fix slow task list endpoint\n"
         "verify_cmd: pytest -q\n\n## Reproduction\nLoad it.\n\n## Scope\n\n## Out of scope\nUI.\n"},
    ),
    "009_triage_split_epic.json": (
        {"triage/epic-export/01-api.md": TASK_MD.format(role="backend", title="Add CSV export endpoint"),
         "triage/epic-export/02-button.md": TASK_MD.format(role="frontend", title="Add export button"),
         "triage/epic-export/03-pii.md": TASK_MD.format(role="review", title="Review export for PII")},
        {"triage/epic-export/01-api.md": TASK_MD.format(role="backend", title="Add CSV export endpoint"),
         "triage/epic-export/02-button.md": TASK_MD.format(role="frontend", title="Add export button"),
         "triage/epic-export/03-more.md": TASK_MD.format(role="backend", title="Add CSV streaming")},
    ),
    "010_backend_rate_limiter.json": (
        {"ratelimit.py": LIMITER,
         "tests/test_ratelimit.py": "from ratelimit import RateLimiter\n\n\ndef test_burst():\n"
         "    r = RateLimiter(1.0, 2)\n    assert [r.allow(0.0) for _ in range(3)] == [True, True, False]\n"},
        {"ratelimit.py": LIMITER.replace("min(self.capacity, ", "(").replace("float(capacity)", "0.0"),
         "tests/test_ratelimit.py": "def test_nothing():\n    assert True\n"},
    ),
}  # fmt: skip


def run_verify(name, files, tmp_path):
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    argv = shlex.split(PACK[name]["verify_cmd"])
    if argv[0] == "python":
        argv[0] = sys.executable
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    return subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)


def test_pack_shape():
    assert len(PACK) == 10
    roles = [t["role"] for t in PACK.values()]
    for role in ("backend", "frontend", "review", "triage"):
        assert roles.count(role) >= 2, role
    hard = [n for n, t in PACK.items() if "swarm-hard" in t.get("labels", [])]
    assert len(hard) == 2
    for name, task in PACK.items():
        assert task["title"] and shlex.split(task["verify_cmd"]), name
        # verify_cmd is exec'd without a shell (sandbox.check_workspace)
        assert not any(tok in ("&&", "||", "|", ";") for tok in shlex.split(task["verify_cmd"]))
        assert ("[swarm-hard]" in task["title"]) == (name in hard)  # router/planner marker
    assert set(FIXTURES) == set(PACK) - {
        "001_healthz_endpoint.json", "002_fix_off_by_one.json", "003_lint_cleanup.json"}  # fmt: skip


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_verify_cmd_accepts_a_correct_workspace(name, tmp_path):
    res = run_verify(name, FIXTURES[name][0], tmp_path)
    assert res.returncode == 0, res.stderr[-800:] + res.stdout[-400:]


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_verify_cmd_rejects_wrong_and_empty_workspaces(name, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_verify(name, {}, empty).returncode != 0
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    assert run_verify(name, FIXTURES[name][1], wrong).returncode != 0


def test_dry_run_lists_ten_tasks_with_roles_and_verify_commands():
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "shadow_eval.py"), "--dry-run"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert "golden pack: 10 task(s)" in out
    assert out.count("role=") == 10 and out.count("    verify: ") == 10
    assert "per role: backend=4, frontend=2, review=2, triage=2" in out
    assert out.count("labels=swarm-hard") == 2


def test_report_has_per_role_deltas():
    def run(label, greens):
        results = [{"name": n, "role": PACK[n]["role"], "green": g, "tokens": 10, "latency_s": 1.0}
                   for n, g in zip(PACK, greens, strict=True)]  # fmt: skip
        return shadow.summarize(label, results)

    default = run("default", [True] * 4 + [False] * 6)
    candidate = run("cand", [True] * 6 + [False] * 4)
    assert default["by_role"]["frontend"] == {"tasks": 2, "green_rate": 0.5}
    report = shadow.markdown_report(default, candidate)
    assert "| role | tasks | default green | candidate green | delta |" in report
    assert "| frontend | 2 | 50% | 100% | +50.0% (better) |" in report
    assert "| triage | 2 | 0% | 0% | +0.0% (same) |" in report
    legacy = {k: v for k, v in default.items() if k != "by_role"}
    assert "| backend |" in shadow.markdown_report(legacy, candidate)  # older saved runs
