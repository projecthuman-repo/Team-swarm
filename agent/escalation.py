"""Escalation lane hand-off (review R11; v4.2 T7.7) — the Postgres side.

A swarm-hard task that exhausts its repair budget (or misses the rubric
twice) is handed to the escalation lane instead of the DLQ — only when
ROUTE_API_FALLBACK=1 (hosted fallback is opt-in and off by default,
INV-027). The hand-off is ONE transaction: the task row moves to
`escalated`, the failing diff and the gate output are stored in
`escalations`, and an outbox row on the dedicated subject announces it.
The relay publishes that row (Hard Rule 2); the host-run lane worker
(agent/escalation_worker.py, `make escalation`) consumes it through its own
durable consumer and claims the task by compare-and-set (Hard Rule 3).

At most one escalation per task: `escalations.task_id` is the primary key
and `tasks.escalated_at` is never cleared, so a second hand-off is refused
and the task takes the DLQ path it would have taken without the flag.

This module has no SDK import: the sealed agent containers run it, and the
Claude Agent SDK is in none of their images.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen"))

from swarm.v1 import event_pb2  # noqa: E402

SUBJECT = "swarm.tasks.escalation"  # in SWARM_TASKS: its failures reach SWARM_DLQ too
DURABLE = "escalation"
MAX_DIFF_CHARS = 400_000  # context for the escalation model, never applied
MAX_OUTPUT_CHARS = 20_000


def enabled() -> bool:
    """Escalation exists only behind the explicit opt-in flag."""
    return os.environ.get("ROUTE_API_FALLBACK", "0") == "1"


def _text(value: str, limit: int) -> str:
    return value.replace("\x00", "")[-limit:]


async def hand_off(
    pool, task, reason: str, failing_diff: str, gate_output: str, agent: str
) -> bool:
    """Hand a claimed task to the escalation lane. False = refused (it was
    escalated before, or this worker no longer holds it) and nothing changed."""
    task_id = uuid.UUID(task.task_id)
    event = event_pb2.AgentEvent(
        task_id=task.task_id,
        agent=agent,
        kind=event_pb2.AgentEvent.PROGRESS,
        detail=f"escalated: {reason}"[:2000],
        ts_unix=int(time.time()),
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE tasks SET status='escalated', escalated_at=now(), updated_at=now() "
                "WHERE task_id=$1 AND status='claimed' AND escalated_at IS NULL "
                "RETURNING task_id",
                task_id,
            )
            if row is None:
                return False
            await conn.execute(
                "INSERT INTO escalations(task_id, reason, failing_diff, gate_output) "
                "VALUES($1, $2, $3, $4)",
                task_id,
                _text(reason, MAX_OUTPUT_CHARS),
                failing_diff.replace("\x00", "")[:MAX_DIFF_CHARS],
                _text(gate_output, MAX_OUTPUT_CHARS),
            )
            await conn.execute(
                "INSERT INTO outbox(subject, msg_id, payload) VALUES($1, $2, $3)",
                SUBJECT,
                f"escalation-{task.task_id}",
                task.SerializeToString(),
            )
            await conn.execute(
                "INSERT INTO outbox(subject, msg_id, payload) VALUES($1, $2, $3)",
                "swarm.events.agent",
                str(uuid.uuid4()),
                event.SerializeToString(),
            )
    return True


async def claim(pool, task_id: str, worker_id: str) -> bool:
    """The lane's atomic claim (Hard Rule 3): escalated -> claimed, one winner."""
    row = await pool.fetchrow(
        "UPDATE tasks SET status='claimed', claimed_by=$2, updated_at=now() "
        "WHERE task_id=$1 AND status='escalated' RETURNING task_id",
        uuid.UUID(task_id),
        worker_id,
    )
    return row is not None


async def finish(pool, task_id: str, outcome: str) -> None:
    await pool.execute(
        "UPDATE escalations SET outcome=$2, finished_at=now() WHERE task_id=$1",
        uuid.UUID(task_id),
        outcome.replace("\x00", "")[:2000],
    )
