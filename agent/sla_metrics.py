"""SLA queries (runbook §17) shared by `scripts/sla_report.py` (`make sla`)
and the agents' `/metrics` endpoint (Lean & Precise L3.2).

Every query takes an asyncpg pool (or connection) and a window in hours.
`collect()` returns the Prometheus gauge values; None means "no data yet"
and is exported as NaN.
"""

from __future__ import annotations

import os

HEADROOM_STATS_URL = os.environ.get("HEADROOM_STATS_URL", "http://localhost:8787")

GAUGES = {
    "swarm_tasks_done_24h_ratio": "share of tasks in the window done within 24h (SLA >= 0.7)",
    "swarm_pickup_latency_p95_seconds": "p95 publish->claim latency (SLA: avg < 600 s)",
    "swarm_first_pass_green_ratio": "first-pass green rate of measured tasks (north star)",
    "swarm_tokens_per_merged_pr": "engine tokens per task that opened its PR (done)",
    "swarm_headroom_savings_ratio": "input tokens saved by the Headroom proxy (1 - post/pre)",
    "swarm_dlq_depth": "tasks failed to the DLQ in the window",
    "swarm_snowball_total": "snowball circuit-breaker terminations in the window",
    "swarm_gate_stage1_ms_median": "median first-pass stage-1 (fast lint) gate time, ms",
    "swarm_outbox_rows": "rows in the transactional outbox",
}


async def completion(pool, hours: int) -> tuple[int, int]:
    """(tasks created in the window, of which done within 24h)."""
    row = await pool.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (
                 WHERE status = 'done'
                 AND updated_at - created_at <= interval '24 hours') AS done
        FROM tasks WHERE created_at > now() - make_interval(hours => $1)
        """,
        hours,
    )
    return row["total"], row["done"]


async def pickup_latency(pool, hours: int):
    """Row with avg_pickup (interval) and p95_s (seconds)."""
    return await pool.fetchrow(
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


async def first_pass(pool, hours: int):
    """v4.1 T2.1 — row with green, measured, avg_repairs."""
    return await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE first_pass_green) AS green,
               count(*) FILTER (WHERE first_pass_green IS NOT NULL) AS measured,
               coalesce(avg(repair_cycles), 0) AS avg_repairs
        FROM tasks WHERE created_at > now() - make_interval(hours => $1)
        """,
        hours,
    )


async def gate_medians(pool, hours: int):
    """L2.1 — row with s1, s2 (median first-pass stage timings, ms)."""
    return await pool.fetchrow(
        """
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY gate_stage1_ms) AS s1,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY gate_stage2_ms) AS s2
        FROM tasks WHERE created_at > now() - make_interval(hours => $1)
        """,
        hours,
    )


async def outbox_rows(pool) -> tuple[int, int]:
    """L2.5 — (rows, unpublished rows)."""
    row = await pool.fetchrow(
        "SELECT count(*) AS n, count(*) FILTER (WHERE published_at IS NULL) AS pending FROM outbox"
    )
    return row["n"], row["pending"]


async def tokens_per_done(pool, hours: int) -> float | None:
    """L3.2 — mean engine tokens of tasks that reached done (PR opened)."""
    return await pool.fetchval(
        "SELECT avg(tokens_spent)::float8 FROM tasks WHERE status = 'done' "
        "AND tokens_spent IS NOT NULL AND created_at > now() - make_interval(hours => $1)",
        hours,
    )


async def dlq_and_snowball(pool, hours: int) -> tuple[int, int]:
    """(tasks failed in the window, of which snowball terminations).

    A failed task is one the worker terminated to the DLQ; the snowball
    breaker writes `reason=snowball` into its FAILED event (outbox payload).
    """
    row = await pool.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM tasks WHERE status = 'failed'
             AND updated_at > now() - make_interval(hours => $1)) AS dlq,
          (SELECT count(*) FROM outbox WHERE subject = 'swarm.events.agent'
             AND position('reason=snowball'::bytea IN payload) > 0
             AND (published_at IS NULL
                  OR published_at > now() - make_interval(hours => $1))) AS snowball
        """,
        hours,
    )
    return row["dlq"], row["snowball"]


def fetch_headroom_stats(url: str = HEADROOM_STATS_URL) -> dict | None:
    """Headroom proxy /stats, or None when unreachable (v4.1 T1.6)."""
    import requests

    try:
        resp = requests.get(f"{url.rstrip('/')}/stats", timeout=3)
        if resp.ok:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def headroom_savings_ratio(stats: dict | None) -> float | None:
    if not stats:
        return None
    pre = stats.get("tokens_pre", stats.get("input_tokens_before", 0))
    post = stats.get("tokens_post", stats.get("input_tokens_after", 0))
    return 1 - post / pre if pre else None


async def collect(pool, hours: int = 24, stats: dict | None = None) -> dict[str, float | None]:
    """Every gauge in GAUGES. `stats` = Headroom /stats (fetched by caller)."""
    total, done = await completion(pool, hours)
    lat = await pickup_latency(pool, hours)
    fp = await first_pass(pool, hours)
    gate = await gate_medians(pool, hours)
    rows, _ = await outbox_rows(pool)
    dlq, snowball = await dlq_and_snowball(pool, hours)
    return {
        "swarm_tasks_done_24h_ratio": done / total if total else None,
        "swarm_pickup_latency_p95_seconds": lat["p95_s"],
        "swarm_first_pass_green_ratio": fp["green"] / fp["measured"] if fp["measured"] else None,
        "swarm_tokens_per_merged_pr": await tokens_per_done(pool, hours),
        "swarm_headroom_savings_ratio": headroom_savings_ratio(stats),
        "swarm_dlq_depth": dlq,
        "swarm_snowball_total": snowball,
        "swarm_gate_stage1_ms_median": gate["s1"],
        "swarm_outbox_rows": rows,
    }
