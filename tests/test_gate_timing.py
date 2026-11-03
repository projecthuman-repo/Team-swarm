"""L2.1 — gate timings flow RunResult -> tasks row -> SLA report."""

import contextlib
import importlib.util
import os
import re

from agent import agent_worker
from agent.sandbox import RunResult

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


class FakeConn:
    def __init__(self, log):
        self.log = log

    async def execute(self, sql, *args):
        self.log.append((sql, args))

    def transaction(self):
        return contextlib.nullcontext()


class FakePool:
    def __init__(self):
        self.log = []

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield FakeConn(self.log)


async def test_handle_task_writes_gate_timings(monkeypatch):
    @contextlib.asynccontextmanager
    async def no_lock(key, ttl):
        yield

    async def fake_run(task, secret, budget):
        return RunResult(first_pass_green=False, repair_cycles=1,
                         gate_stage1_ms=412, gate_stage2_ms=None)  # fmt: skip

    async def fake_emit(*a, **k):
        return None

    monkeypatch.setattr(agent_worker, "valkey_lock", no_lock)
    monkeypatch.setattr(agent_worker, "lease_secret", lambda name: "leased")
    monkeypatch.setattr(agent_worker, "run_sandboxed_aider", fake_run)
    monkeypatch.setattr(agent_worker, "emit", fake_emit)
    pool = FakePool()
    task = agent_worker.task_pb2.Task(task_id="00000000-0000-0000-0000-000000000001",
                                      role="backend", title="synthetic")  # fmt: skip
    await agent_worker.handle_task(pool, task, budget=None)
    sql, args = pool.log[0]
    assert "gate_stage1_ms=$4" in sql and "gate_stage2_ms=$5" in sql
    assert "tokens_spent=$6" in sql  # L3.2 swarm_tokens_per_merged_pr source
    assert args[1:] == (False, 1, 412, None, 0)


def test_migration_and_schema_carry_the_columns():
    mig = open(os.path.join(ROOT, "db", "migrations", "006_gate_timing.sql")).read()
    schema = open(os.path.join(ROOT, "db", "schema.sql")).read()
    for col in ("gate_stage1_ms", "gate_stage2_ms"):
        assert re.search(rf"ADD COLUMN IF NOT EXISTS {col} int", mig)
        assert re.search(rf"^\s+{col}\s+int,", schema, re.M)


def test_sla_gate_line():
    spec = importlib.util.spec_from_file_location(
        "sla_report_l21", os.path.join(ROOT, "scripts", "sla_report.py")
    )
    sla = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sla)
    assert sla.gate_timing_line(1500.0, 42000.0).endswith("1.5s / 42.0s  (fast lint / full checks)")
    assert "n/a / n/a" in sla.gate_timing_line(None, None)
