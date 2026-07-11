"""Speculative multi-candidate patches, gated (v4.1 T2.5 / B7).

For swarm-hard tasks ONLY: generate N=3 candidate patches with a
temperature/approach spread, run the T2.1 green-build check on each, and
select by green result. A hard budget check runs BEFORE fan-out (each
candidate costs a full engine run); cost per candidate is logged.

    python scripts/speculative_patch.py <task_id>   # task must be claimed/hard
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent", "gen"))

from swarm.v1 import task_pb2  # noqa: E402

from agent.budget import ValkeyBudget  # noqa: E402
from agent.openbao import lease_secret  # noqa: E402
from agent.sandbox import run_sandboxed_aider  # noqa: E402

log = logging.getLogger("speculative")

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)
N_CANDIDATES = int(os.environ.get("SPECULATIVE_CANDIDATES", "3"))

# Approach spread stands in for raw temperature where the engine doesn't
# expose it: each candidate is nudged toward a different solution shape.
APPROACH_HINTS = [
    "Prefer the smallest possible diff; change as few lines as you can.",
    "Prefer the clearest implementation, even if slightly longer.",
    "Prefer reusing existing helpers in the repo over writing new code.",
]


def select_candidate(results: list[dict]) -> dict | None:
    """Pick the sole/first green candidate; None when all red.

    results: [{candidate, green, tokens, branch}]
    """
    green = [r for r in results if r.get("green")]
    if not green:
        return None
    # cheapest green wins when several pass
    return sorted(green, key=lambda r: r.get("tokens", 0))[0]


def budget_allows_fanout(spent: int, limit: int, n: int) -> bool:
    """Hard pre-check (B7): fan-out only if the remaining budget covers a
    conservative estimate of n candidate runs."""
    if not limit:
        return True
    est_per_candidate = max(spent, limit // 10)
    return spent + n * est_per_candidate <= limit


async def run(task_id: str) -> int:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    row = await pool.fetchrow(
        "SELECT title, budget_tokens FROM tasks WHERE task_id=$1",
        uuid.UUID(task_id),
    )
    if not row:
        raise SystemExit(f"unknown task {task_id}")

    budget = ValkeyBudget("speculative", task_id, row["budget_tokens"])
    spent = await budget.spent()
    if not budget_allows_fanout(spent, row["budget_tokens"], N_CANDIDATES):
        raise SystemExit(
            f"budget check failed before fan-out: {spent} spent of "
            f"{row['budget_tokens']} cannot cover {N_CANDIDATES} candidates"
        )

    secret = lease_secret("forgejo")
    results = []
    for i in range(N_CANDIDATES):
        cand_branch = f"feature/{task_id}-cand{i}"
        task = task_pb2.Task(
            task_id=task_id,
            role="backend",
            title=f"{row['title']}\n\nApproach: {APPROACH_HINTS[i % len(APPROACH_HINTS)]}",
            branch=cand_branch,
            budget_tokens=row["budget_tokens"],
        )
        try:
            res = await run_sandboxed_aider(task, secret, budget=budget)
            results.append(
                {
                    "candidate": i,
                    "green": True,
                    "tokens": res.tokens_spent,
                    "branch": cand_branch,
                }
            )
        except Exception as err:
            results.append(
                {"candidate": i, "green": False, "tokens": 0, "error": str(err)}
            )
        log.info("candidate %d: %s (cost=%s tokens)",
                 i, results[-1].get("green"), results[-1].get("tokens"))

    winner = select_candidate(results)
    for r in results:
        print(f"candidate {r['candidate']}: green={r.get('green')} "
              f"tokens={r.get('tokens')} branch={r.get('branch', '-')}")
    if winner is None:
        print("no green candidate — task stays with the normal repair/DLQ path")
        return 1
    print(f"selected candidate {winner['candidate']} ({winner['branch']})")
    return 0


def main() -> None:
    logging.basicConfig(level="INFO")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("task_id")
    sys.exit(asyncio.run(run(p.parse_args().task_id)))


if __name__ == "__main__":
    main()
