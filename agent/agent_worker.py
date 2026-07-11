"""Agent worker — one process per role (backend|frontend|review|triage).

Event-driven consumer of the SWARM_TASKS work-queue stream:

  1. Fetch a task from the role's durable pull consumer (no busy-polling).
  2. Claim it atomically in Postgres (compare-and-set). If another instance
     already claimed it, ack and move on — exactly one owner per task.
  3. Take a Valkey lock + budget, lease the Forgejo token from OpenBao,
     and run aider (architect/editor on Ornith-1.0) inside the sandbox.
  4. Emit the state change AND the AgentEvent outbox row in ONE Postgres
     transaction. The relay is the only publisher into JetStream.

Failure semantics (Hard Rules / runbook §8):
  - transient failure (budget/timeout) -> nak(delay=30)  -> JetStream retry
    with backoff, up to --max-deliver=5.
  - poison task -> term() -> MAX_DELIVERIES advisory -> SWARM_DLQ.
  - worker crash mid-task -> message un-acked -> redelivered after ack-wait
    to another instance.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid

import asyncpg
import nats
from nats.errors import TimeoutError as NatsTimeoutError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen"))

from swarm.v1 import event_pb2, task_pb2  # noqa: E402

from agent.budget import (  # noqa: E402
    BudgetExceeded,
    CircuitBreaker,
    SnowballError,
    ValkeyBudget,
    valkey_lock,
)
from agent.health import serve_health  # noqa: E402
from agent.openbao import lease_secret  # noqa: E402
from agent.sandbox import PoisonError, run_sandboxed_aider  # noqa: E402
from agent.tracing import emit_trace  # noqa: E402

log = logging.getLogger("agent")

ROLE = os.environ.get("AGENT_ROLE", "backend")  # backend|frontend|review|triage
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)
FETCH_TIMEOUT_S = int(os.environ.get("FETCH_TIMEOUT_S", "30"))
# v4.1 T4.2 — tier-aware scheduling. "default" keeps the v4.0 subject
# swarm.tasks.<role>; extra tiers subscribe to swarm.tasks.<role>.<tier>.
# WORKER_TIERS=auto derives serveable tiers from the capacity probe.
WORKER_TIERS = os.environ.get("WORKER_TIERS", "default")
WORKER_ID = f"{ROLE}-{uuid.uuid4().hex[:8]}"


async def pg_claim(pool: asyncpg.Pool, task_id: str) -> bool:
    """Atomic compare-and-set claim (Hard Rule 3). One RETURNING row wins."""
    row = await pool.fetchrow(
        """
        UPDATE tasks SET status='claimed', claimed_by=$2, updated_at=now()
        WHERE task_id=$1 AND status='pending' RETURNING task_id
        """,
        uuid.UUID(task_id),
        WORKER_ID,
    )
    return row is not None


async def emit(
    pool: asyncpg.Pool,
    task_id: str,
    kind: int,
    status: str,
    detail: str = "",
) -> None:
    """Write the task state change and the event outbox row in ONE tx.

    Never publishes to JetStream directly — the relay does (Hard Rule 2).
    """
    ev = event_pb2.AgentEvent(
        task_id=task_id,
        agent=ROLE,
        kind=kind,
        detail=detail,
        ts_unix=int(time.time()),
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


async def handle_task(
    pool: asyncpg.Pool, task: task_pb2.Task, budget: ValkeyBudget
) -> None:
    ttl = max(60, int(task.deadline_unix - time.time())) if task.deadline_unix else 3600
    if os.environ.get("CONTEXT_PACK", "") == "1":  # v4.1 T2.4 bounded RAG
        from agent.context_pack import build_context

        task.title += await build_context(pool, task.title, ROLE)
    async with valkey_lock(f"task:{task.task_id}", ttl=ttl):
        secret = lease_secret("forgejo")  # leased, short TTL, never persisted
        result = await run_sandboxed_aider(task, secret, budget=budget)
        async with pool.acquire() as conn:  # v4.1 T2.1 quality metrics
            await conn.execute(
                "UPDATE tasks SET first_pass_green=$2, repair_cycles=$3 "
                "WHERE task_id=$1",
                uuid.UUID(task.task_id),
                result.first_pass_green,
                result.repair_cycles,
            )
        await emit(
            pool,
            task.task_id,
            event_pb2.AgentEvent.PR_OPENED,
            status="done",
            detail=f"branch={task.branch or f'feature/{task.task_id}'}",
        )


async def task_created_at(pool: asyncpg.Pool, task_id: str) -> float:
    row = await pool.fetchrow(
        "SELECT extract(epoch FROM created_at) AS ts FROM tasks WHERE task_id=$1",
        uuid.UUID(task_id),
    )
    return float(row["ts"]) if row else time.time()


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("worker %s starting (role=%s)", WORKER_ID, ROLE)

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()

    # v4.1 T4.1 — register this node's capacity for tier-aware routing.
    from agent.capacity import register_node, serveable_tiers

    caps = await register_node(pool, WORKER_ID)
    tiers = (
        serveable_tiers(caps)
        if WORKER_TIERS == "auto"
        else [t.strip() for t in WORKER_TIERS.split(",") if t.strip()]
    )

    # One durable consumer per tier; "default" keeps the v4.0 subject.
    subs = []
    for tier in tiers:
        if tier == "default":
            subs.append(await js.pull_subscribe(f"swarm.tasks.{ROLE}", durable=ROLE))
        else:
            subs.append(
                await js.pull_subscribe(
                    f"swarm.tasks.{ROLE}.{tier}", durable=f"{ROLE}-{tier}"
                )
            )
    log.info("subscribed tiers: %s", tiers)

    health = asyncio.create_task(serve_health(WORKER_ID, ROLE))

    while True:  # event-driven pull, not fixed-interval poll
        msgs = []
        for sub in subs:
            try:
                msgs = await sub.fetch(1, timeout=FETCH_TIMEOUT_S // len(subs) or 1)
                if msgs:
                    break
            except NatsTimeoutError:
                continue  # nothing pending on this tier; try the next

        for m in msgs:
            task = task_pb2.Task.FromString(m.data)
            claimed = await pg_claim(pool, task.task_id)
            if not claimed:
                # Another instance owns it (or it's already done): drop ours.
                await m.ack()
                continue

            log.info("claimed task %s (%s)", task.task_id, task.title)
            emit_trace("claimed", task.task_id, ROLE, detail=task.title)
            budget = ValkeyBudget(
                agent=ROLE, task_id=task.task_id, limit=task.budget_tokens
            )
            breaker = CircuitBreaker(
                task.task_id, await task_created_at(pool, task.task_id)
            )
            try:
                await breaker.check(time.time())  # T5.3: trip before spending
                await handle_task(pool, task, budget)
                await m.ack()
                emit_trace(
                    "done",
                    task.task_id,
                    ROLE,
                    detail=task.title,
                    tokens_spent=await budget.spent(),
                )
                log.info("task %s done", task.task_id)
            except (BudgetExceeded, TimeoutError) as err:
                log.warning("task %s transient failure: %s", task.task_id, err)
                emit_trace("retry", task.task_id, ROLE, detail=str(err))
                await pool.execute(
                    "UPDATE tasks SET status='pending', attempt=attempt+1, "
                    "claimed_by=NULL, updated_at=now() WHERE task_id=$1",
                    uuid.UUID(task.task_id),
                )
                await m.nak(delay=30)  # retry with backoff
            except SnowballError as err:
                # T5.3: expensive-failure circuit breaker -> DLQ, reason
                # recorded so morning triage sees the snowball explicitly.
                log.error("task %s snowballed: %s", task.task_id, err)
                emit_trace("failed", task.task_id, ROLE, detail=f"reason=snowball {err}")
                await emit(
                    pool,
                    task.task_id,
                    event_pb2.AgentEvent.FAILED,
                    status="failed",
                    detail=f"reason=snowball {err}",
                )
                await m.term()
            except PoisonError as err:
                log.error("task %s poisoned: %s", task.task_id, err)
                emit_trace("failed", task.task_id, ROLE, detail=str(err))
                await emit(
                    pool,
                    task.task_id,
                    event_pb2.AgentEvent.FAILED,
                    status="failed",
                    detail=str(err),
                )
                await m.term()  # -> MAX_DELIVERIES advisory -> SWARM_DLQ

    health.cancel()


if __name__ == "__main__":
    asyncio.run(main())
