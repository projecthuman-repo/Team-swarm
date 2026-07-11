"""DLQ -> human triage path (runbook §17, Stage 4 requirement).

Consumes MAX_DELIVERIES advisories from SWARM_DLQ, resolves each to its
original task, marks the task failed in Postgres, and opens a Forgejo
issue labeled `needs-human` so a person re-scopes the rule/guardrail/skill
(teacher, not doer). Any task landing here for a non-transient reason is a
pause-autonomy signal.

Usage:  python scripts/dlq_triage.py                      # run once, drain, exit
        python scripts/dlq_triage.py --watch              # keep consuming
        python scripts/dlq_triage.py --replay <stream_seq> # re-inject a fixed
            task from the MAX_DELIVERIES advisory's sequence (v4.1 T5.1):
            resets it to pending and writes a fresh outbox row — the relay
            republishes it (Hard Rule 2 preserved).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid

import asyncpg
import nats
import requests
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import ConsumerConfig

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent", "gen"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarm.v1 import task_pb2  # noqa: E402

log = logging.getLogger("dlq-triage")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)
FORGEJO = os.environ.get("FORGEJO_URL", "http://localhost:3000")
OWNER = os.environ.get("FORGEJO_OWNER", "swarm")
REPO = os.environ.get("FORGEJO_REPO", "sandbox-repo")


def open_forgejo_issue(token: str, task: task_pb2.Task, deliveries: int) -> None:
    resp = requests.post(
        f"{FORGEJO}/api/v1/repos/{OWNER}/{REPO}/issues",
        headers={"Authorization": f"token {token}"},
        json={
            "title": f"[DLQ] task {task.task_id}: {task.title}",
            "body": (
                f"Task `{task.task_id}` (role `{task.role}`) exhausted "
                f"{deliveries} deliveries and reached SWARM_DLQ.\n\n"
                "Per the autonomy rules: fix the rule/guardrail/skill that "
                "let this happen — not the code. Pause overnight runs if "
                "the cause is non-transient."
            ),
            "labels": [],
        },
        timeout=30,
    )
    resp.raise_for_status()
    log.info("opened Forgejo issue #%s", resp.json().get("number"))


async def triage(watch: bool) -> None:
    from agent.openbao import lease_secret

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    sub = await js.pull_subscribe(
        "",
        durable="dlq-triage",
        stream="SWARM_DLQ",
        config=ConsumerConfig(durable_name="dlq-triage"),
    )
    try:
        token = lease_secret("forgejo")
    except Exception as err:  # still mark tasks failed without Forgejo
        log.warning("no Forgejo token available (%s); issues will be skipped", err)
        token = None

    while True:
        try:
            msgs = await sub.fetch(10, timeout=5)
        except NatsTimeoutError:
            if watch:
                continue
            log.info("DLQ drained")
            break
        for m in msgs:
            advisory = json.loads(m.data)
            seq = advisory.get("stream_seq")
            deliveries = advisory.get("deliveries", 0)
            raw = await js.get_msg("SWARM_TASKS", seq)
            task = task_pb2.Task.FromString(raw.data)
            log.warning(
                "DLQ task %s (%s) after %d deliveries",
                task.task_id,
                task.title,
                deliveries,
            )
            await pool.execute(
                "UPDATE tasks SET status='failed', updated_at=now() WHERE task_id=$1",
                uuid.UUID(task.task_id),
            )
            if token:
                open_forgejo_issue(token, task, deliveries)
            await m.ack()
    await nc.drain()


async def replay(stream_seq: int) -> None:
    """v4.1 T5.1 — DLQ replay drill: re-inject a fixed task by sequence."""
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    raw = await js.get_msg("SWARM_TASKS", stream_seq)
    task = task_pb2.Task.FromString(raw.data)
    task.attempt += 1
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE tasks SET status='pending', claimed_by=NULL, "
                "attempt=attempt+1, updated_at=now() WHERE task_id=$1",
                uuid.UUID(task.task_id),
            )
            await conn.execute(
                "INSERT INTO outbox(subject, msg_id, payload) VALUES($1, $2, $3)",
                raw.subject,
                f"replay-{stream_seq}-{uuid.uuid4().hex[:8]}",  # fresh dedupe key
                task.SerializeToString(),
            )
    log.info("replayed task %s (seq %d) via the outbox", task.task_id, stream_seq)
    await nc.drain()
    await pool.close()


def main() -> None:
    logging.basicConfig(level="INFO", format="%(name)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--watch", action="store_true", help="keep consuming")
    p.add_argument("--replay", type=int, metavar="STREAM_SEQ",
                   help="re-inject a fixed task from SWARM_TASKS by sequence")
    args = p.parse_args()
    if args.replay:
        asyncio.run(replay(args.replay))
    else:
        asyncio.run(triage(args.watch))


if __name__ == "__main__":
    main()
