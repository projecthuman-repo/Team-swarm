"""Operational SLA report (runbook §17). Tuning signals, not hard stops:

  - task completion rate: >=70% of tasks marked done within 24h
  - inter-agent message latency (publish -> claim): average < 10 min
  - p95 task-pickup latency (the Stage-3 scale metric: should stay flat
    scaling 4 -> 16 concurrent agents)

Falling below points at heartbeat/consumer config, prompts, or embeddings.
Vector-search relevance remains a periodic manual spot-check
(scripts/seed_knowledge.py --query "...").

Usage:  python scripts/sla_report.py [--hours 24]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)


async def report(hours: int) -> int:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)

    total, done_in_window = await pool.fetchrow(
        """
        SELECT count(*),
               count(*) FILTER (
                 WHERE status = 'done'
                 AND updated_at - created_at <= interval '24 hours')
        FROM tasks WHERE created_at > now() - make_interval(hours => $1)
        """,
        hours,
    )

    latency = await pool.fetchrow(
        """
        SELECT avg(t.updated_at - o.published_at)                       AS avg_pickup,
               percentile_cont(0.95) WITHIN GROUP
                 (ORDER BY extract(epoch FROM t.updated_at - o.published_at)) AS p95_s
        FROM tasks t
        JOIN outbox o ON o.msg_id = t.task_id::text
        WHERE t.status IN ('claimed', 'done')
          AND o.published_at IS NOT NULL
          AND t.created_at > now() - make_interval(hours => $1)
        """,
        hours,
    )

    rate = (done_in_window / total * 100) if total else 100.0
    avg_pickup = latency["avg_pickup"]
    p95_s = latency["p95_s"]

    print(f"window: last {hours}h | tasks: {total}")
    print(f"completion within 24h : {rate:.0f}%  (SLA >= 70%)")
    print(
        "avg publish->claim    : "
        f"{avg_pickup if avg_pickup is not None else 'n/a'}  (SLA < 10 min)"
    )
    print(
        "p95 pickup latency    : "
        f"{f'{p95_s:.1f}s' if p95_s is not None else 'n/a'}"
        "  (Stage 3: flat 4 -> 16 agents)"
    )

    breached = rate < 70 or (
        avg_pickup is not None and avg_pickup.total_seconds() > 600
    )
    if breached:
        print("SLA BREACH — tune consumers/prompts/embeddings (not a hard stop)")
    await pool.close()
    return 1 if breached else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hours", type=int, default=24)
    sys.exit(asyncio.run(report(p.parse_args().hours)))


if __name__ == "__main__":
    main()
