"""Escalation lane worker (review R11; v4.2 T7.7) — HOST-RUN, opt-in.

    ROUTE_API_FALLBACK=1 make escalation      # uv sync --group agent --extra escalation

Never part of a sealed agent container and never baked into an image: it
needs Anthropic egress (Hard Rule 1 keeps agents off the internet) and the
Claude Agent SDK's wheel bundles a CLI under commercial terms, which no
image closure may contain (conventions/licensing.md).

One task at a time, from the durable consumer `escalation` on
`swarm.tasks.escalation`:

  1. claim it by compare-and-set (escalated -> claimed);
  2. read the failing diff and the gate output the normal path stored;
  3. escalate() -> a patch (the key is an OpenBao lease);
  4. apply it to a fresh clone of the base branch and run the same
     two-stage gate (sandbox.run_escalated_patch);
  5. green: push feature/<task_id> and emit PR_OPENED exactly as the normal
     path does, marked escalated in the event and the trace (the task row
     already carries escalated_at);
     anything else — the patch does not apply, the gate is red or times
     out, the SDK is not installed or fails, a key lease fails — FAILED
     with the reason (status `failed`, the event, `escalations.outcome`)
     and term(); nothing is pushed.

There is no second escalation: a task reaches this lane at most once
(agent/escalation.py), and this worker never hands anything back.

The gate executes model-written code on this host, outside the container
seal. It runs without the swarm's secrets and service addresses
(sandbox.lane_gate_env) and the bot token is not in the clone; run the lane
on a host where that is acceptable.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid

import asyncpg
import nats
from nats.errors import TimeoutError as NatsTimeoutError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen"))

from swarm.v1 import event_pb2, task_pb2  # noqa: E402

from agent import escalation  # noqa: E402
from agent.agent_worker import DATABASE_URL, NATS_URL, require_gate_tools  # noqa: E402
from agent.escalation_claude import EscalationUnavailable, escalate  # noqa: E402
from agent.openbao import lease_secret  # noqa: E402
from agent.sandbox import PoisonError, extract_patch, run_escalated_patch  # noqa: E402
from agent.tracing import emit_trace  # noqa: E402

log = logging.getLogger("escalation")

ROLE = "escalation"
WORKER_ID = f"{ROLE}-{uuid.uuid4().hex[:8]}"
FETCH_TIMEOUT_S = int(os.environ.get("FETCH_TIMEOUT_S", "30"))


async def emit(pool, task_id: str, kind: int, status: str, detail: str) -> None:
    """State change + outbox event in one transaction (Hard Rule 2), with
    this lane as the event's agent."""
    import time

    ev = event_pb2.AgentEvent(
        task_id=task_id, agent=ROLE, kind=kind, detail=detail[:2000], ts_unix=int(time.time())
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE tasks SET status=$2, updated_at=now() WHERE task_id=$1",
                uuid.UUID(task_id),
                status,
            )
            await conn.execute(
                "INSERT INTO outbox(subject, msg_id, payload) VALUES($1, $2, $3)",
                "swarm.events.agent",
                str(uuid.uuid4()),
                ev.SerializeToString(),
            )


async def handle(pool, task, escalate_fn=escalate, lease=lease_secret) -> bool:
    """Run one claimed escalation to its end. True = PR path, False = DLQ.

    Never raises for a task-level failure: every outcome is recorded on the
    task row, in `escalations` and as an outbox event.
    """
    try:
        row = await pool.fetchrow(
            "SELECT failing_diff, gate_output FROM escalations WHERE task_id=$1",
            uuid.UUID(task.task_id),
        )
        if row is None:
            raise PoisonError("no escalation record for this task")
        try:
            reply = await escalate_fn(task.title, row["failing_diff"], row["gate_output"])
        except EscalationUnavailable as err:
            raise PoisonError(f"escalation unavailable: {err}") from err
        try:
            secret = lease("forgejo")
        except Exception as err:  # noqa: BLE001 - any lease failure is the DLQ reason
            raise PoisonError(f"could not lease the Forgejo token: {err}") from err
        result = await run_escalated_patch(task, secret, extract_patch(reply))
    except Exception as err:  # noqa: BLE001 - whatever went wrong, the task must not
        # stay `claimed` with no record: the lane has no retry, so every failure
        # ends here, as FAILED with its reason.
        detail = str(err) if isinstance(err, PoisonError) else f"{type(err).__name__}: {err}"
        reason = f"reason=escalation-failed {detail}"
        log.error("task %s: %s", task.task_id, reason)
        await escalation.finish(pool, task.task_id, detail)
        await emit(pool, task.task_id, event_pb2.AgentEvent.FAILED, "failed", reason)
        emit_trace("failed", task.task_id, ROLE, detail=reason)
        return False

    await pool.execute(
        "UPDATE tasks SET gate_stage1_ms=$2, gate_stage2_ms=$3 WHERE task_id=$1",
        uuid.UUID(task.task_id),
        result.gate_stage1_ms,
        result.gate_stage2_ms,
    )
    await escalation.finish(pool, task.task_id, "pr_opened")
    detail = f"branch={result.branch} escalated=1"
    await emit(pool, task.task_id, event_pb2.AgentEvent.PR_OPENED, "done", detail)
    emit_trace("done", task.task_id, ROLE, detail=f"escalated {task.title}")
    log.info("task %s done (escalated)", task.task_id)
    return True


async def process(pool, m, escalate_fn=escalate, lease=lease_secret) -> bool | None:
    """One message from the escalation consumer, to its end.

    None: not ours (another instance holds the task, or it is finished) —
    acked. True: PR path — acked. False: failed — term(), no redelivery;
    the record is the FAILED event and `escalations.outcome`.
    """
    task = task_pb2.Task.FromString(m.data)
    if not await escalation.claim(pool, task.task_id, WORKER_ID):
        await m.ack()
        return None
    log.info("claimed escalated task %s (%s)", task.task_id, task.title)
    emit_trace("claimed", task.task_id, ROLE, detail=f"escalated {task.title}")
    if await handle(pool, task, escalate_fn, lease):
        await m.ack()
        return True
    await m.term()
    return False


async def main(once: bool = False) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if not escalation.enabled():
        raise SystemExit(
            "ROUTE_API_FALLBACK is not 1: the escalation lane is opt-in and off by default "
            "(INV-027). Nothing was consumed."
        )
    require_gate_tools()
    log.info("escalation lane %s starting (host-run)", WORKER_ID)

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    nc = await nats.connect(NATS_URL)
    sub = await nc.jetstream().pull_subscribe(escalation.SUBJECT, durable=escalation.DURABLE)

    while True:
        try:
            msgs = await sub.fetch(1, timeout=FETCH_TIMEOUT_S)
        except NatsTimeoutError:
            if once:
                log.error("no escalation arrived within %ss", FETCH_TIMEOUT_S)
                return 1
            continue
        for m in msgs:
            outcome = await process(pool, m)
            if once and outcome is not None:
                return 0 if outcome else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="handle one escalation and exit")
    sys.exit(asyncio.run(main(once=parser.parse_args().once)))
