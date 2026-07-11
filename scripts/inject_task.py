"""Inject synthetic task(s) through the outbox — for dev and qualification.

Writes a tasks row + outbox row in ONE transaction, exactly like the real
ingesters (Hard Rule 2: nothing publishes to JetStream except the relay).

Usage:
    python scripts/inject_task.py --role backend --title "add /healthz endpoint"
    python scripts/inject_task.py --count 100          # qualification stage 1
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent", "gen"))
from swarm.v1 import task_pb2  # noqa: E402

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)
DEFAULT_BUDGET = int(os.environ.get("DEFAULT_BUDGET_TOKENS", "200000"))


async def inject(role: str, title: str, count: int) -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    for i in range(count):
        tid = str(uuid.uuid4())
        t = f"{title} [{i + 1}/{count}]" if count > 1 else title
        task = task_pb2.Task(
            task_id=tid,
            role=role,
            title=t,
            branch=f"feature/{tid}",
            budget_tokens=DEFAULT_BUDGET,
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO tasks(task_id, role, status, title, branch, "
                    "budget_tokens) VALUES($1, $2, 'pending', $3, $4, $5)",
                    uuid.UUID(tid),
                    role,
                    t,
                    f"feature/{tid}",
                    DEFAULT_BUDGET,
                )
                await conn.execute(
                    "INSERT INTO outbox(subject, msg_id, payload) VALUES($1, $2, $3)",
                    f"swarm.tasks.{role}",
                    tid,
                    task.SerializeToString(),
                )
        print(f"injected {role} task {tid}: {t}")
    await pool.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", default="backend",
                   choices=["backend", "frontend", "review", "triage"])
    p.add_argument("--title", default="synthetic qualification task")
    p.add_argument("--count", type=int, default=1)
    args = p.parse_args()
    asyncio.run(inject(args.role, args.title, args.count))


if __name__ == "__main__":
    main()
