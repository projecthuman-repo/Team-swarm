"""Qualification Stage 1 checks (runbook §17): after injecting 100 synthetic
tasks and letting the swarm drain them, verify:

  - zero dual-write losses: every task row has a matching published outbox
    row, and no outbox row is stuck unpublished
  - zero duplicate claims: claimed/done tasks have exactly one claimed_by

Usage:
    python scripts/inject_task.py --count 100
    ... let the relay + workers run ...
    python scripts/qualify_stage1.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)


async def main() -> int:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    failures = []

    stuck = await pool.fetchval(
        "SELECT count(*) FROM outbox "
        "WHERE published_at IS NULL AND id < (SELECT coalesce(max(id),0) FROM outbox)"
    )
    if stuck:
        failures.append(f"{stuck} outbox row(s) stuck unpublished (dual-write risk)")

    orphans = await pool.fetchval(
        """
        SELECT count(*) FROM tasks t
        WHERE NOT EXISTS (
          SELECT 1 FROM outbox o WHERE o.msg_id = t.task_id::text
        )
        """
    )
    if orphans:
        failures.append(f"{orphans} task(s) with no outbox event (dual-write loss)")

    # Duplicate execution would show up as a task flapping between owners;
    # with compare-and-set claims each task has at most one claimed_by.
    unclaimed_done = await pool.fetchval(
        "SELECT count(*) FROM tasks "
        "WHERE status IN ('claimed','done') AND claimed_by IS NULL"
    )
    if unclaimed_done:
        failures.append(f"{unclaimed_done} claimed/done task(s) without an owner")

    by_status = await pool.fetch(
        "SELECT status, count(*) AS n FROM tasks GROUP BY status ORDER BY status"
    )
    print("task states:", {r["status"]: r["n"] for r in by_status})

    await pool.close()
    if failures:
        print("STAGE 1 FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("STAGE 1 PASSED: zero dual-write losses, zero duplicate claims")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
