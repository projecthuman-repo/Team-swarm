"""Review R11 — the three v4.2 capabilities wired into the task flow:
escalation (T7.7), the rubric (T7.8) and Codex as the fourth reviewer (T7.4).

All three are off by default and fail closed. Escalation tests use a fake
`escalate`, a real Postgres (the `pg` fixture) and a local bare repository
standing in for Forgejo; the gate they run is the real two-stage gate.
"""

import contextlib
import glob
import importlib.util
import os
import subprocess
import sys
import types
import uuid

import pytest
import yaml

from agent import agent_worker, escalation, escalation_worker, review_gate, sandbox, verifier
from agent.escalation_claude import EscalationUnavailable
from agent.sandbox import EscalationHandoff, PoisonError

event_pb2, task_pb2 = agent_worker.event_pb2, agent_worker.task_pb2  # agent/gen

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")  # fmt: skip

GOOD_PATCH = """Here is the corrected change.

```diff
diff --git a/slug.py b/slug.py
new file mode 100644
--- /dev/null
+++ b/slug.py
@@ -0,0 +1,6 @@
+import re
+
+
+def slugify(text):
+    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
+
diff --git a/test_slug.py b/test_slug.py
new file mode 100644
--- /dev/null
+++ b/test_slug.py
@@ -0,0 +1,5 @@
+from slug import slugify
+
+
+def test_slugify():
+    assert slugify("Hello, World!") == "hello-world"
```
"""
RED_PATCH = GOOD_PATCH.replace('== "hello-world"', '== "not-what-it-returns"')
STALE_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def subtract(a, b):
-    return a - b
+    return b - a
"""


def text(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


class Pool:
    """asyncpg.Pool's surface over the one test connection."""

    def __init__(self, conn):
        self.conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn

    def __getattr__(self, name):
        return getattr(self.conn, name)


def new_task(title="[swarm-hard] add slugify", **kw):
    tid = str(uuid.uuid4())
    return task_pb2.Task(task_id=tid, role="backend", title=title, branch=f"feature/{tid}", **kw)


async def insert(pg, task, status="claimed", labels=()):
    await pg.execute(
        "INSERT INTO tasks(task_id, role, status, claimed_by, title, branch, "
        "budget_tokens, labels) VALUES($1, 'backend', $2, 'backend-test', $3, $4, 1000, $5)",
        uuid.UUID(task.task_id),
        status,
        task.title,
        task.branch,
        list(labels),
    )


async def task_row(pg, task):
    return await pg.fetchrow("SELECT * FROM tasks WHERE task_id=$1", uuid.UUID(task.task_id))


async def events(pg, task):
    rows = await pg.fetch("SELECT payload FROM outbox WHERE subject='swarm.events.agent'")
    parsed = [event_pb2.AgentEvent.FromString(r["payload"]) for r in rows]
    return [e for e in parsed if e.task_id == task.task_id]


async def handoffs(pg):
    return await pg.fetch("SELECT * FROM outbox WHERE subject=$1", escalation.SUBJECT)


@pytest.fixture
def forgejo(tmp_path, monkeypatch):
    """A bare repository as the remote, with one commit on its default branch."""
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (seed / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    bare = tmp_path / "remote.git"
    for cmd in (
        ["git", "init", "-q", "-b", "integration"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "init"],
        ["git", "clone", "-q", "--bare", ".", str(bare)],
    ):
        subprocess.run(cmd, cwd=seed, env=GIT_ENV, check=True)
    seen = []

    def remote(secret):
        seen.append(secret)
        return f"file://{bare}"

    monkeypatch.setattr(sandbox, "_remote", remote)
    monkeypatch.setattr(sandbox, "SANDBOX_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")
    return types.SimpleNamespace(path=bare, secrets=seen)


def branches(bare):
    out = subprocess.run(
        ["git", "branch", "--list", "--format=%(refname:short)"],
        cwd=bare.path, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return set(out.split())


def show(bare, ref, path):
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=bare.path, capture_output=True, text=True
    ).stdout


def fake_escalate(reply, calls=None):
    async def escalate(spec, failing_diff, gate_output):
        if calls is not None:
            calls.append((spec, failing_diff, gate_output))
        if isinstance(reply, Exception):
            raise reply
        return reply

    return escalate


# --- escalation: the hand-off ------------------------------------------------


async def test_hand_off_stores_diff_and_gate_output_with_the_outbox_event(pg):
    pool, task = Pool(pg), new_task()
    await insert(pg, task)
    assert await escalation.hand_off(pool, task, "red after 2", "the diff", "the gate output", "b")
    row = await task_row(pg, task)
    assert row["status"] == "escalated" and row["escalated_at"] is not None
    stored = await pg.fetchrow("SELECT * FROM escalations")
    assert (stored["failing_diff"], stored["gate_output"]) == ("the diff", "the gate output")
    assert stored["reason"] == "red after 2" and stored["outcome"] is None
    (announce,) = await handoffs(pg)
    assert announce["subject"] == "swarm.tasks.escalation"
    assert announce["msg_id"] == f"escalation-{task.task_id}"
    assert task_pb2.Task.FromString(announce["payload"]).task_id == task.task_id
    assert announce["published_at"] is None  # the relay publishes it, never the worker
    (audit,) = await events(pg, task)
    assert audit.kind == event_pb2.AgentEvent.PROGRESS and audit.detail.startswith("escalated:")


async def test_hand_off_is_one_transaction(pg):
    pool, task = Pool(pg), new_task()
    await insert(pg, task)
    # make the last statement of the hand-off fail: its msg_id is taken
    await pg.execute(
        "INSERT INTO outbox(subject, msg_id, payload) VALUES('x', $1, '\\x00')",
        f"escalation-{task.task_id}",
    )
    with pytest.raises(Exception, match="outbox_msg_id"):
        await escalation.hand_off(pool, task, "r", "d", "g", "b")
    assert (await task_row(pg, task))["status"] == "claimed"  # rolled back with the rest
    assert (await task_row(pg, task))["escalated_at"] is None
    assert await pg.fetchval("SELECT count(*) FROM escalations") == 0
    assert await handoffs(pg) == []


async def test_a_second_escalation_is_refused(pg):
    pool, task = Pool(pg), new_task()
    await insert(pg, task)
    assert await escalation.hand_off(pool, task, "first", "d", "g", "b")
    # the task comes back to a normal worker (an operator re-injected it) and fails again
    await pg.execute("UPDATE tasks SET status='claimed'")
    err = EscalationHandoff("second", "d2", "g2")
    assert await agent_worker.try_hand_off(pool, task, err) is False  # -> the DLQ path
    assert await pg.fetchval("SELECT count(*) FROM escalations") == 1
    assert len(await handoffs(pg)) == 1
    assert (await task_row(pg, task))["status"] == "claimed"  # untouched by the refusal


async def test_hand_off_needs_a_task_this_worker_holds(pg):
    pool, task = Pool(pg), new_task()
    await insert(pg, task, status="done")
    assert await escalation.hand_off(pool, task, "r", "d", "g", "b") is False
    assert await handoffs(pg) == []


async def test_a_plain_failure_is_never_handed_off(pg):
    pool, task = Pool(pg), new_task()
    await insert(pg, task)
    assert await agent_worker.try_hand_off(pool, task, PoisonError("red after 2")) is False
    assert await handoffs(pg) == [] and await pg.fetchval("SELECT count(*) FROM escalations") == 0
    assert (await task_row(pg, task))["status"] == "claimed"


# --- escalation: who is eligible ----------------------------------------------


class NoDatabase:
    def __getattr__(self, name):
        raise AssertionError(f"the database was touched ({name}) with every flag off")


@pytest.fixture
def flags(monkeypatch):
    def set_flags(fallback="0", rubric=False, codex=False, required=False):
        monkeypatch.setenv("ROUTE_API_FALLBACK", fallback)
        monkeypatch.setattr(review_gate, "RUBRIC_ENABLED", rubric)
        monkeypatch.setattr(review_gate, "CODEX_REVIEW", codex)
        monkeypatch.setattr(review_gate, "CODEX_REVIEW_REQUIRED", required)

    set_flags()
    return set_flags


async def test_flags_off_is_the_flow_it_always_was(flags, monkeypatch):
    task = new_task()
    assert await agent_worker.task_flow(NoDatabase(), task) == (False, None)

    @contextlib.asynccontextmanager
    async def no_lock(key, ttl):
        yield

    seen = {}

    async def run(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        raise PoisonError("red after 2 repair cycle(s); not pushing.")

    monkeypatch.setattr(agent_worker, "valkey_lock", no_lock)
    monkeypatch.setattr(agent_worker, "lease_secret", lambda name: "leased")
    monkeypatch.setattr(agent_worker, "run_sandboxed_aider", run)
    with pytest.raises(PoisonError) as failure:
        await agent_worker.handle_task(NoDatabase(), task, budget="b")
    assert type(failure.value) is PoisonError  # not a hand-off: the DLQ, exactly as before
    assert seen["args"] == (task, "leased") and seen["kwargs"] == {"budget": "b"}


@pytest.mark.parametrize(
    "fallback, title, labels, expected",
    [
        ("1", "add slugify", [], False),  # flag on, label absent
        ("0", "add slugify", ["swarm-hard"], False),  # label present, flag off
        ("1", "add slugify", ["swarm-hard"], True),
        ("1", "[swarm-hard] add slugify", [], True),  # the title marker counts as the label
        ("1", "add slugify", ["swarm-ready", "role:backend"], False),
    ],
)
async def test_escalation_needs_the_flag_and_the_label(
    pg, flags, fallback, title, labels, expected
):
    flags(fallback=fallback, rubric=fallback == "0")  # some flag on, so the row is read
    task = new_task(title=title)
    await insert(pg, task, labels=labels)
    escalate, _ = await agent_worker.task_flow(Pool(pg), task)
    assert escalate is expected


def test_issue_labels_are_stored_on_the_task_row():
    from ingest.forgejo_issue_sync import labels_of

    issue = {"labels": [{"name": "swarm-ready"}, {"name": "swarm-hard"}]}
    assert labels_of(issue) == ["swarm-ready", "swarm-hard"]
    assert labels_of({}) == []
    source = text("ingest", "forgejo_issue_sync.py")
    assert "budget_tokens, labels) " in source and "labels_of(it)," in source


# --- escalation: the sandbox raises the hand-off only when asked --------------


class Budget:
    def __init__(self):
        self.spent = 0

    async def spend(self, n):
        self.spent += n


@pytest.fixture
def engine(monkeypatch):
    """A scripted engine: each run executes the next snippet in the workspace."""
    script = []
    prompts = []

    def engine_cmd(prompt):
        prompts.append(prompt)
        code = script.pop(0) if script else "pass"
        return [sys.executable, "-c", code]

    monkeypatch.setattr(sandbox, "engine_cmd", engine_cmd)
    monkeypatch.setattr(sandbox, "MAX_REPAIR_CYCLES", 1)
    engine_cmd.script, engine_cmd.prompts = script, prompts
    return engine_cmd


WRITE_RED = "open('test_new.py','w').write('def test_new():\\n    assert False\\n')"
WRITE_GREEN = "open('test_new.py','w').write('def test_new():\\n    assert True\\n')"


async def test_red_after_repair_is_a_plain_poison_error_by_default(forgejo, engine):
    engine.script[:] = [WRITE_RED, WRITE_RED]
    task = new_task()
    with pytest.raises(PoisonError) as failure:
        await sandbox.run_sandboxed_aider(task, "tok", budget=Budget())
    assert type(failure.value) is PoisonError
    assert str(failure.value).startswith("red after 1 repair cycle(s); not pushing. Last check")
    assert task.branch not in branches(forgejo)


async def test_red_after_repair_becomes_a_hand_off_when_asked(forgejo, engine):
    engine.script[:] = [WRITE_RED, WRITE_RED]
    task = new_task()
    with pytest.raises(EscalationHandoff) as failure:
        await sandbox.run_sandboxed_aider(task, "tok", budget=Budget(), escalate=True)
    assert str(failure.value).startswith("red after 1 repair cycle(s); not pushing.")
    assert "+def test_new():" in failure.value.failing_diff  # the untracked file is in the diff
    assert "__pycache__" not in failure.value.failing_diff
    assert "assert False" in failure.value.gate_output
    assert task.branch not in branches(forgejo)  # a red gate never pushes


# --- escalation: the lane ------------------------------------------------------


async def staged(pg, title="[swarm-hard] add slugify"):
    pool, task = Pool(pg), new_task(title=title)
    await insert(pg, task, labels=["swarm-hard"])
    assert await escalation.hand_off(pool, task, "red after 2", "old diff", "old gate output", "b")
    return pool, task


async def test_lane_claim_is_compare_and_set(pg):
    pool, task = await staged(pg)
    assert await escalation.claim(pool, task.task_id, "escalation-1") is True
    assert await escalation.claim(pool, task.task_id, "escalation-2") is False  # one winner
    assert (await task_row(pg, task))["claimed_by"] == "escalation-1"
    other = new_task()
    await insert(pg, other, status="pending")
    assert await escalation.claim(pool, other.task_id, "escalation-1") is False  # never handed off


async def test_good_patch_runs_the_gate_and_takes_the_pr_path(pg, forgejo, monkeypatch):
    pool, task = await staged(pg)
    assert await escalation.claim(pool, task.task_id, "escalation-1")
    gate_runs, traces, calls = [], [], []
    real_gate = sandbox.check_workspace

    async def gate(repo, env, verify_cmd="", timings=None, base="HEAD"):
        url = subprocess.run(["git", "config", "remote.origin.url"], cwd=repo,
                             capture_output=True, text=True).stdout  # fmt: skip
        gate_runs.append((env, url))
        return await real_gate(repo, env, verify_cmd, timings, base)

    monkeypatch.setattr(sandbox, "check_workspace", gate)
    monkeypatch.setattr(escalation_worker, "emit_trace", lambda *a, **k: traces.append((a, k)))
    monkeypatch.setenv("OPENBAO_TOKEN", "root-token")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/db")

    ok = await escalation_worker.handle(
        pool, task, escalate_fn=fake_escalate(GOOD_PATCH, calls), lease=lambda name: "leased-tok"
    )

    assert ok is True
    assert calls == [(task.title, "old diff", "old gate output")]  # what the normal path stored
    assert len(gate_runs) == 1  # the same two-stage gate, once
    gate_env, stored_remote = gate_runs[0]
    assert "OPENBAO_TOKEN" not in gate_env and "DATABASE_URL" not in gate_env
    assert "leased-tok" not in stored_remote  # the token is not in the clone the gate runs in
    assert forgejo.secrets[0] == "leased-tok"  # ...it is used for the clone and the push only
    assert task.branch in branches(forgejo)  # pushed, feature/* only
    assert "def slugify" in show(forgejo, task.branch, "slug.py")
    row = await task_row(pg, task)
    assert row["status"] == "done" and row["escalated_at"] is not None  # marked on the task row
    assert row["gate_stage2_ms"] is not None
    assert await pg.fetchval("SELECT outcome FROM escalations") == "pr_opened"
    opened = [e for e in await events(pg, task) if e.kind == event_pb2.AgentEvent.PR_OPENED]
    assert len(opened) == 1 and opened[0].detail == f"branch={task.branch} escalated=1"
    assert opened[0].agent == "escalation"
    assert traces[-1][0][0] == "done" and "escalated" in traces[-1][1]["detail"]  # ...and the trace


@pytest.mark.parametrize(
    "reply, reason",
    [
        (RED_PATCH, "escalated patch is red; not pushing"),
        (STALE_PATCH, "escalation patch does not apply; not pushing"),
        ("I could not find a fix.", "escalation returned no patch"),
        (EscalationUnavailable("claude-agent-sdk not installed"), "escalation unavailable"),
        (EscalationUnavailable("could not lease Claude key: 403"), "could not lease Claude key"),
    ],
)
async def test_a_failed_escalation_goes_to_the_dlq_and_pushes_nothing(pg, forgejo, reply, reason):
    pool, task = await staged(pg)
    assert await escalation.claim(pool, task.task_id, "escalation-1")
    ok = await escalation_worker.handle(
        pool, task, escalate_fn=fake_escalate(reply), lease=lambda name: "leased-tok"
    )
    assert ok is False  # -> term(): the DLQ
    assert branches(forgejo) == {"integration"}  # nothing pushed
    assert (await task_row(pg, task))["status"] == "failed"
    failed = [e for e in await events(pg, task) if e.kind == event_pb2.AgentEvent.FAILED]
    assert len(failed) == 1 and reason in failed[0].detail  # the DLQ carries the reason
    assert reason in await pg.fetchval("SELECT outcome FROM escalations")


async def test_a_failed_forgejo_lease_goes_to_the_dlq(pg, forgejo):
    pool, task = await staged(pg)

    def no_lease(name):
        raise RuntimeError("OpenBao sealed")

    ok = await escalation_worker.handle(pool, task, fake_escalate(GOOD_PATCH), no_lease)
    assert ok is False and branches(forgejo) == {"integration"}
    assert "could not lease the Forgejo token: OpenBao sealed" in (
        await pg.fetchval("SELECT outcome FROM escalations")
    )


async def test_the_lane_refuses_to_start_without_the_flag(monkeypatch):
    monkeypatch.setenv("ROUTE_API_FALLBACK", "0")
    with pytest.raises(SystemExit, match="opt-in and off by default"):
        await escalation_worker.main(once=True)


def test_extract_patch_and_the_gate_environment():
    assert sandbox.extract_patch(GOOD_PATCH).startswith("diff --git a/slug.py b/slug.py\n")
    assert sandbox.extract_patch("prose\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b").endswith("+b\n")
    assert sandbox.extract_patch("no diff here") == ""
    env = {"PATH": "/bin", "HOME": "/h", "OPENBAO_TOKEN": "t", "ANTHROPIC_API_KEY": "k",
           "LITE_SMOKE_FORGEJO_TOKEN": "t", "DATABASE_URL": "u", "NATS_URL": "n",
           "LANGFUSE_SECRET_KEY": "s", "PGPASSWORD": "p"}  # fmt: skip
    assert sandbox.lane_gate_env(env) == {"PATH": "/bin", "HOME": "/h"}


def test_the_claude_key_never_enters_the_lane_process_environment(monkeypatch):
    from agent import escalation_claude as esc

    class Options:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Result:
        is_error = False
        result = "diff --git a/x b/x"

    seen = {}

    class Sdk:
        ClaudeAgentOptions = Options

        @staticmethod
        async def query(prompt, options):
            seen["options"] = options
            yield Result()

    monkeypatch.setattr(esc, "ROUTE_API_FALLBACK", True)
    monkeypatch.setattr(esc, "_load_sdk", lambda: Sdk)
    monkeypatch.setattr("agent.openbao.lease_secret", lambda name, field="token": "sk-leased")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import asyncio

    reply = asyncio.run(esc.escalate("spec", "diff", "checks"))
    assert reply == "diff --git a/x b/x"
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert seen["options"].env == {"ANTHROPIC_API_KEY": "sk-leased"}
    assert seen["options"].tools == []  # a patch as text: no file or shell tools on the host


# --- the rubric (T7.8) -----------------------------------------------------------


def rubric(verdict, score=0.25):
    return {"score": score, "verdict": verdict, "notes": f"2. no tests updated ({verdict})"}


class Seen(list):
    """The bundles the grader saw; `.grades` is its script."""


@pytest.fixture
def graded(monkeypatch, flags):
    """RUBRIC_ENABLED=1 with a scripted grader; returns the list of bundles it saw."""
    flags(rubric=True)
    grades, bundles = [], Seen()

    def grade(bundle):
        bundles.append(bundle)
        return grades.pop(0)

    monkeypatch.setattr(verifier, "grade_rubric", grade)
    monkeypatch.setattr(verifier, "verify", lambda bundle: pytest.fail("lenses on a plain task"))
    bundles.grades = grades
    return bundles


async def run_reviewed(pg, forgejo, engine, hard=False, can_escalate=False):
    title = "[swarm-hard] add a test" if hard else "add a test"
    task = new_task(title=title)
    await insert(pg, task)
    gate = review_gate.ReviewGate(Pool(pg), task.task_id, hard, can_escalate)
    run = sandbox.run_sandboxed_aider(
        task, "tok", budget=Budget(), review=gate, escalate=can_escalate
    )
    return task, run


async def test_rubric_hit_pushes_without_a_revision(pg, forgejo, engine, graded):
    graded.grades[:] = [rubric("HIT", 1.0)]
    engine.script[:] = [WRITE_GREEN]
    task, run = await run_reviewed(pg, forgejo, engine)
    await run
    assert len(engine.prompts) == 1 and len(graded) == 1
    assert "# Diff under review" in graded[0] and "+def test_new():" in graded[0]
    assert (await task_row(pg, task))["rubric_misses"] == 0


async def test_first_miss_forces_exactly_one_revision_with_the_notes(pg, forgejo, engine, graded):
    graded.grades[:] = [rubric("MISS"), rubric("HIT", 1.0)]
    engine.script[:] = [WRITE_GREEN, WRITE_GREEN]
    task, run = await run_reviewed(pg, forgejo, engine)
    result = await run
    assert len(engine.prompts) == 2  # the first run and one revision
    assert "RUBRIC REVISION (one round only)" in engine.prompts[1]
    assert "2. no tests updated (MISS)" in engine.prompts[1]  # the grader's notes, verbatim
    assert "rubric score 0.25" in engine.prompts[1]
    assert (await task_row(pg, task))["rubric_misses"] == 1  # the counter lives on the task row
    assert result.first_pass_green is True  # the metric is still the first pass


async def test_second_miss_takes_the_needs_human_path_never_a_third_round(
    pg, forgejo, engine, graded
):
    graded.grades[:] = [rubric("MISS"), rubric("MISS"), rubric("HIT", 1.0)]
    engine.script[:] = [WRITE_GREEN, WRITE_GREEN, WRITE_GREEN]
    task, run = await run_reviewed(pg, forgejo, engine)
    with pytest.raises(PoisonError) as failure:
        await run
    assert type(failure.value) is PoisonError  # needs-human: the DLQ
    assert "rubric MISS twice" in str(failure.value) and "not pushing" in str(failure.value)
    assert len(engine.prompts) == 2 and len(graded) == 2  # never a third round
    assert (await task_row(pg, task))["rubric_misses"] == 2
    assert task.branch not in branches(forgejo)


async def test_second_miss_escalates_when_escalation_is_enabled_for_the_task(
    pg, forgejo, engine, graded, monkeypatch
):
    monkeypatch.setattr(verifier, "verify", lambda bundle: {"findings": []})
    graded.grades[:] = [rubric("MISS"), rubric("MISS")]
    engine.script[:] = [WRITE_GREEN, WRITE_GREEN]
    task, run = await run_reviewed(pg, forgejo, engine, hard=True, can_escalate=True)
    with pytest.raises(EscalationHandoff) as failure:
        await run
    assert "rubric MISS twice" in str(failure.value)
    assert "+def test_new():" in failure.value.failing_diff
    assert "2. no tests updated (MISS)" in failure.value.gate_output
    assert len(engine.prompts) == 2 and task.branch not in branches(forgejo)


async def test_a_redelivered_task_cannot_buy_another_revision(pg, forgejo, engine, graded):
    graded.grades[:] = [rubric("MISS")]
    engine.script[:] = [WRITE_GREEN]
    task, run = await run_reviewed(pg, forgejo, engine)
    await pg.execute("UPDATE tasks SET rubric_misses = 1")  # its revision was spent before
    with pytest.raises(PoisonError, match="rubric MISS twice"):
        await run
    assert len(engine.prompts) == 1


async def test_a_revision_that_turns_the_gate_red_is_not_graded_again(pg, forgejo, engine, graded):
    graded.grades[:] = [rubric("MISS")]
    engine.script[:] = [WRITE_GREEN, WRITE_RED, WRITE_RED]
    task, run = await run_reviewed(pg, forgejo, engine)
    with pytest.raises(PoisonError, match="red after 1 repair cycle"):
        await run
    assert len(graded) == 1 and task.branch not in branches(forgejo)


def test_rubric_is_off_by_default():
    assert importlib.util.find_spec("agent.review_gate") is not None
    source = text("agent", "review_gate.py")
    for flag in ("RUBRIC_ENABLED", "CODEX_REVIEW", "CODEX_REVIEW_REQUIRED"):
        assert f'{flag} = os.environ.get("{flag}", "0") == "1"' in source


# --- Codex, the fourth reviewer (T7.4) --------------------------------------------


def lens(name, verdict="PASS"):
    return {"lens": name, "verdict": verdict, "notes": f"{name} notes"}


@pytest.fixture
def reviewers(monkeypatch, flags):
    calls = []
    three = [lens("architect"), lens("security"), lens("qa")]
    monkeypatch.setattr(verifier, "verify", lambda bundle: {"findings": list(three)})

    def install(codex_finding):
        from agent import codex_review

        def codex(base, cwd="."):
            calls.append((base, cwd))
            return [codex_finding]

        monkeypatch.setattr(codex_review, "review", codex)

    install.calls = calls
    return install


def test_codex_findings_join_the_aggregate_for_swarm_hard_reviews(reviewers, flags):
    flags(codex=True)
    reviewers({"lens": "codex", "verdict": "PASS", "notes": "no findings"})
    result = review_gate.review("bundle", hard=True, base="abc123", cwd="/repo")
    assert [f["lens"] for f in result["findings"]] == ["architect", "security", "qa", "codex"]
    assert result["verdict"] == "PASS" and reviewers.calls == [("abc123", "/repo")]


def test_a_codex_block_blocks(reviewers, flags):
    flags(codex=True)
    reviewers({"lens": "codex", "verdict": "BLOCK", "notes": "P0: SQL injection"})
    result = review_gate.review("bundle", hard=True)
    assert result["verdict"] == "BLOCK" and [f["lens"] for f in result["blocking"]] == ["codex"]


def test_a_codex_error_is_recorded_and_does_not_block_by_default(reviewers, flags):
    flags(codex=True)
    reviewers({"lens": "codex", "verdict": "ERROR", "notes": "codex CLI not installed"})
    result = review_gate.review("bundle", hard=True)
    assert result["verdict"] == "PASS" and result["blocking"] == []
    assert result["findings"][-1] == {
        "lens": "codex", "verdict": "ERROR", "notes": "codex CLI not installed"}  # fmt: skip


def test_a_codex_error_blocks_when_required(reviewers, flags):
    flags(codex=True, required=True)
    reviewers({"lens": "codex", "verdict": "ERROR", "notes": "unparseable output"})
    result = review_gate.review("bundle", hard=True)
    assert result["verdict"] == "BLOCK" and result["blocking"][0]["lens"] == "codex"


def test_a_lens_error_still_blocks_and_codex_stays_out_unless_asked(reviewers, flags):
    reviewers({"lens": "codex", "verdict": "BLOCK", "notes": "must not be called"})
    assert review_gate.review("bundle", hard=True)["verdict"] == "PASS"  # CODEX_REVIEW off
    assert reviewers.calls == []
    flags(codex=True)
    assert review_gate.review("bundle", hard=False)["findings"] == []  # not a swarm-hard review
    assert reviewers.calls == []
    findings = [lens("architect", "ERROR"), lens("codex", "ERROR")]
    result = verifier.aggregate(findings, advisory_errors=("codex",))
    assert [f["lens"] for f in result["blocking"]] == ["architect"]  # lenses stay fail-closed
    assert verifier.aggregate(findings)["verdict"] == "BLOCK"  # the default is unchanged


def test_the_review_step_exists_only_when_a_flag_asks(flags):
    assert review_gate.wanted(hard=True) is False and review_gate.wanted(hard=False) is False
    flags(codex=True)
    assert review_gate.wanted(hard=True) is True and review_gate.wanted(hard=False) is False
    flags(rubric=True)
    assert review_gate.wanted(hard=False) is True


async def test_a_blocked_review_is_never_pushed(pg, forgejo, engine, reviewers, flags):
    flags(codex=True)
    reviewers({"lens": "codex", "verdict": "BLOCK", "notes": "P0: SQL injection"})
    engine.script[:] = [WRITE_GREEN]
    task, run = await run_reviewed(pg, forgejo, engine, hard=True)
    with pytest.raises(PoisonError, match=r"independent review blocked \(codex\); not pushing"):
        await run
    assert task.branch not in branches(forgejo) and len(engine.prompts) == 1


# --- migration, lane packaging, documentation -------------------------------------


async def test_migration_011_is_idempotent_on_real_postgres(pg):
    task = new_task()
    await insert(pg, task, labels=["swarm-hard"])
    await pg.execute("UPDATE tasks SET rubric_misses = 1")
    assert await escalation.hand_off(Pool(pg), task, "r", "d", "g", "b")
    migrations = sorted(glob.glob(os.path.join(ROOT, "db", "migrations", "*.sql")))
    assert os.path.basename(migrations[-1]) == "011_escalation.sql"
    for _ in range(2):
        for path in migrations:
            with open(path) as f:
                await pg.execute(f.read())
    row = await task_row(pg, task)
    assert (row["labels"], row["rubric_misses"]) == (["swarm-hard"], 1)  # data survives
    assert row["escalated_at"] is not None
    assert await pg.fetchval("SELECT count(*) FROM escalations") == 1
    with pytest.raises(Exception, match="escalations_pkey"):  # at most one per task
        await pg.execute(
            "INSERT INTO escalations(task_id, reason, failing_diff, gate_output) "
            "VALUES($1, 'r', 'd', 'g')",
            uuid.UUID(task.task_id),
        )


async def test_migration_011_alone_upgrades_a_v41_tasks_table(pg):
    await pg.execute("DROP TABLE escalations")
    await pg.execute(
        "ALTER TABLE tasks DROP COLUMN labels, DROP COLUMN rubric_misses, DROP COLUMN escalated_at"
    )
    await pg.execute(text("db", "migrations", "011_escalation.sql"))
    task = new_task()
    await insert(pg, task)
    assert await escalation.hand_off(Pool(pg), task, "r", "d", "g", "b")
    row = await task_row(pg, task)
    assert row["labels"] == [] and row["rubric_misses"] == 0


def test_schema_mirrors_migration_011():
    schema, migration = text("db", "schema.sql"), text("db", "migrations", "011_escalation.sql")
    for needle in ("labels", "rubric_misses", "escalated_at"):
        assert f"ADD COLUMN IF NOT EXISTS {needle} " in migration and f"  {needle} " in schema
    table = migration[migration.index("CREATE TABLE IF NOT EXISTS escalations") :]
    assert table.strip() in schema


def _license_gate():
    spec = importlib.util.spec_from_file_location(
        "license_gate_r11", os.path.join(ROOT, ".forgejo", "scripts", "license_gate.py")
    )
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def test_no_image_closure_contains_the_claude_agent_sdk():
    gate = _license_gate()
    for image, selection in gate.IMAGES.items():
        assert "--extra" not in selection and "--all-extras" not in selection, image
    closures = gate.closures()  # the real `uv export` of every image the swarm builds
    assert set(closures) == set(gate.IMAGES) and all(closures.values())
    for image, packages in closures.items():
        names = {name for name, _ in packages}
        assert "claude-agent-sdk" not in names, image
    locked = {name for name, _ in gate.locked_packages()}
    assert "claude-agent-sdk" in locked  # it is locked — for the host-run lane only


def test_the_lane_is_host_run_and_in_no_container():
    for path in glob.glob(os.path.join(ROOT, "deploy", "**", "*"), recursive=True):
        if os.path.isfile(path) and not path.endswith((".png", ".json")):
            with open(path, errors="replace") as f:
                body = f.read()
            assert "escalation_worker" not in body and "--extra escalation" not in body, path
    out = subprocess.run(
        ["make", "-n", "--no-print-directory", "escalation"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert out.strip() == "./scripts/escalation_lane.sh"
    lane = text("scripts", "escalation_lane.sh")
    assert "uv sync -q --frozen --no-install-project --group agent --extra escalation" in lane
    assert "-m agent.escalation_worker" in lane
    # the hand-off module runs in the sealed containers: it must not need the SDK
    assert "claude_agent_sdk" not in text("agent", "escalation.py")
    assert "claude_agent_sdk" not in text("agent", "agent_worker.py")


def test_the_escalation_subject_has_its_own_durable_consumer():
    bootstrap = text("scripts", "bootstrap_jetstream.sh")
    assert "add_consumer escalation swarm.tasks.escalation" in bootstrap
    assert (escalation.SUBJECT, escalation.DURABLE) == ("swarm.tasks.escalation", "escalation")
    assert "--subjects='swarm.tasks.>'" in bootstrap  # the stream that feeds SWARM_DLQ covers it
    worker = text("agent", "escalation_worker.py")
    assert "pull_subscribe(escalation.SUBJECT, durable=escalation.DURABLE)" in worker
    assert "js.publish" not in worker and ".publish(" not in text("agent", "escalation.py")


def run_step(tmp_path, *command):
    env = dict(os.environ, VI_LOG=str(tmp_path / "vi.log"),
               HOST_MODE_FILE=str(tmp_path / "none"))  # fmt: skip
    subprocess.run([os.path.join(ROOT, "scripts", "vi_step.sh"), "step", *command],
                   env=env, check=True, capture_output=True)  # fmt: skip
    summary = subprocess.run([os.path.join(ROOT, "scripts", "vi_summary.sh")],
                             env=env, capture_output=True, text=True)  # fmt: skip
    return (tmp_path / "vi.log").read_text(), summary


def test_the_real_escalation_step_is_skipped_with_a_clear_message_when_off(tmp_path):
    env = dict(os.environ, ROUTE_API_FALLBACK="0")
    smoke = subprocess.run([os.path.join(ROOT, "scripts", "escalation_lane.sh"), "--smoke"],
                           env=env, capture_output=True, text=True)  # fmt: skip
    assert smoke.returncode == 77
    assert "SKIP: ROUTE_API_FALLBACK is not 1" in smoke.stdout and "INV-027" in smoke.stdout
    recipe = text("Makefile")
    assert "./scripts/escalation_lane.sh --smoke" in recipe.split("verify-integration:")[1]
    log, summary = run_step(tmp_path, "sh", "-c", "exit 77")
    assert log == "SKIP\tstep\n" and summary.returncode == 0
    assert "1 skipped" in summary.stdout
    log, summary = run_step(tmp_path, "sh", "-c", "exit 1")
    assert log.endswith("FAIL\tstep\n") and summary.returncode == 1  # a failure is still a failure


def test_new_variables_targets_and_flows_are_documented():
    env_example = text(".env.example")
    for variable in ("RUBRIC_ENABLED", "CODEX_REVIEW", "CODEX_REVIEW_REQUIRED",
                     "ROUTE_API_FALLBACK", "ESCALATION_ENV"):  # fmt: skip
        assert variable in env_example, variable
    assert "ROUTE_API_FALLBACK=0" in env_example  # off by default
    assert "| `make escalation` |" in text("README.md") or "`make escalation`" in text("README.md")
    runbook = text("docs", "RUNBOOK.md")
    for needle in ("011_escalation.sql", "make escalation", "swarm.tasks.escalation",
                   "RUBRIC_ENABLED", "CODEX_REVIEW_REQUIRED"):  # fmt: skip
        assert needle in runbook, needle
    convention = text("conventions", "escalation.md")
    for needle in (
        "host-run",
        "most one escalation per task",
        "swarm-hard",
        "needs-human",
        "ROUTE_API_FALLBACK",
    ):
        assert needle in convention, needle
    services = yaml.safe_load(text("deploy", "compose.yaml"))["services"]
    assert not any("escalation" in name for name in services)
