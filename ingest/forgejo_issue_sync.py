"""Forgejo issues -> tasks (the PRIMARY task entry point, runbook §11A).

Runs on a schedule (Forgejo Actions cron or host cron). Lists open issues
labeled `swarm-ready`; for each new one, inserts a tasks row PLUS an outbox
row in ONE transaction — nothing reaches JetStream except via the relay
(Hard Rule 2). Role comes from a `role:<name>` label (default `triage`);
dedupe is on ext_id (the Forgejo issue number), so re-runs are idempotent.

Usage:  python -m ingest.forgejo_issue_sync
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid

import asyncpg
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent", "gen"))

from swarm.v1 import task_pb2  # noqa: E402

log = logging.getLogger("issue-sync")

FORGEJO = os.environ.get("FORGEJO_URL", "http://forgejo:3000")
OWNER = os.environ.get("FORGEJO_OWNER", "swarm")
REPO = os.environ.get("FORGEJO_REPO", "sandbox-repo")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)
DEFAULT_BUDGET = int(os.environ.get("DEFAULT_BUDGET_TOKENS", "200000"))
ROLES = {"backend", "frontend", "review", "triage"}


def fetch_swarm_ready_issues(token: str) -> list[dict]:
    resp = requests.get(
        f"{FORGEJO}/api/v1/repos/{OWNER}/{REPO}/issues",
        params={"labels": "swarm-ready", "state": "open"},
        headers={"Authorization": f"token {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def role_of(issue: dict) -> str:
    role = next(
        (
            label["name"].split(":", 1)[1]
            for label in issue.get("labels", [])
            if label["name"].startswith("role:")
        ),
        "triage",
    )
    return role if role in ROLES else "triage"


async def ingest(issues: list[dict]) -> int:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    created = 0
    for it in issues:
        ext_id = f"forgejo#{it['number']}"
        role = role_of(it)
        async with pool.acquire() as conn:
            async with conn.transaction():
                if await conn.fetchval(
                    "SELECT 1 FROM tasks WHERE ext_id=$1", ext_id
                ):
                    continue  # already ingested (idempotent re-run)
                tid = str(uuid.uuid4())
                await conn.execute(
                    "INSERT INTO tasks(task_id, role, status, ext_id, title, "
                    "branch, budget_tokens) VALUES($1, $2, 'pending', $3, $4, $5, $6)",
                    uuid.UUID(tid),
                    role,
                    ext_id,
                    it["title"],
                    f"feature/{tid}",
                    DEFAULT_BUDGET,
                )
                task = task_pb2.Task(
                    task_id=tid,
                    role=role,
                    title=it["title"],
                    branch=f"feature/{tid}",
                    budget_tokens=DEFAULT_BUDGET,
                )
                await conn.execute(
                    "INSERT INTO outbox(subject, msg_id, payload) "
                    "VALUES($1, $2, $3)",
                    f"swarm.tasks.{role}",
                    tid,
                    task.SerializeToString(),
                )
                created += 1
                log.info("ingested %s as %s task %s", ext_id, role, tid)
    await pool.close()
    return created


def main() -> None:
    logging.basicConfig(level="INFO", format="%(name)s %(levelname)s %(message)s")
    from agent.openbao import lease_secret

    token = lease_secret("forgejo")
    issues = fetch_swarm_ready_issues(token)
    created = asyncio.run(ingest(issues))
    log.info("sync complete: %d issue(s) seen, %d task(s) created", len(issues), created)


if __name__ == "__main__":
    main()
