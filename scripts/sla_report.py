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
HEADROOM_STATS_URL = os.environ.get("HEADROOM_STATS_URL", "http://localhost:8787")
OUTPUT_HOLDOUT = float(os.environ.get("HEADROOM_OUTPUT_HOLDOUT", "0") or 0)


def headroom_savings_lines(stats: dict | None, holdout: float) -> list[str]:
    """v4.1 T1.6 — savings lines, honestly labelled measured vs estimated.

    With an output-shaper holdout (>0), a control slice runs unshaped so
    the output-savings number is MEASURED against it; without a holdout
    the number is an ESTIMATE and is labelled as such.
    """
    if not stats:
        return ["headroom savings      : n/a (proxy not reachable)"]
    pre = stats.get("tokens_pre", stats.get("input_tokens_before", 0))
    post = stats.get("tokens_post", stats.get("input_tokens_after", 0))
    lines = []
    if pre:
        lines.append(
            f"headroom input saved  : {1 - post / max(pre, 1):.0%} "
            f"({pre} -> {post} tokens) [measured at proxy]"
        )
    out_saved = stats.get("output_tokens_saved", 0)
    label = (
        f"[measured vs {holdout:.0%} unshaped holdout]"
        if holdout > 0
        else "[estimated — set HEADROOM_OUTPUT_HOLDOUT>0 to measure]"
    )
    lines.append(f"headroom output saved : {out_saved} tokens {label}")
    return lines


def print_headroom_savings() -> None:
    import requests

    stats = None
    try:
        resp = requests.get(f"{HEADROOM_STATS_URL.rstrip('/')}/stats", timeout=3)
        if resp.ok:
            stats = resp.json()
    except requests.RequestException:
        pass
    for line in headroom_savings_lines(stats, OUTPUT_HOLDOUT):
        print(line)


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

    # v4.1 T2.1 — first-pass green rate (north-star metric DR-4.1)
    green_row = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE first_pass_green) AS green,
               count(*) FILTER (WHERE first_pass_green IS NOT NULL) AS measured,
               coalesce(avg(repair_cycles), 0) AS avg_repairs
        FROM tasks WHERE created_at > now() - make_interval(hours => $1)
        """,
        hours,
    )

    rate = (done_in_window / total * 100) if total else 100.0
    avg_pickup = latency["avg_pickup"]
    p95_s = latency["p95_s"]

    print(f"window: last {hours}h | tasks: {total}")
    print(f"completion within 24h : {rate:.0f}%  (SLA >= 70%)")
    if green_row["measured"]:
        green_rate = green_row["green"] / green_row["measured"] * 100
        print(
            f"first-pass green rate : {green_rate:.0f}% of {green_row['measured']} "
            f"measured (avg {green_row['avg_repairs']:.1f} repair cycles)"
        )
    else:
        print("first-pass green rate : n/a (no measured tasks yet)")
    print(
        "avg publish->claim    : "
        f"{avg_pickup if avg_pickup is not None else 'n/a'}  (SLA < 10 min)"
    )
    print(
        "p95 pickup latency    : "
        f"{f'{p95_s:.1f}s' if p95_s is not None else 'n/a'}"
        "  (Stage 3: flat 4 -> 16 agents)"
    )

    print_headroom_savings()

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
