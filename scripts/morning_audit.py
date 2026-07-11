"""Morning verifier audit (v4.1 T6.2 / B3).

Part of the Morning Operator Routine: the review agent re-audits last
night's `integration` merges from a fresh perspective (agent/verifier.py
— no author context). Findings become lessons(tag='morning-audit') and
`needs-human` Forgejo issues, so a seeded bad merge is flagged the next
morning.

    python scripts/morning_audit.py [--since-hours 24]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os
import sys

import asyncpg
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.lessons import write_lesson  # noqa: E402
from agent.verifier import verify  # noqa: E402

log = logging.getLogger("morning-audit")

FORGEJO = os.environ.get("FORGEJO_URL", "http://localhost:3000")
OWNER = os.environ.get("FORGEJO_OWNER", "swarm")
REPO = os.environ.get("FORGEJO_REPO", "sandbox-repo")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)


def merged_integration_prs(token: str, since: datetime.datetime) -> list[dict]:
    resp = requests.get(
        f"{FORGEJO}/api/v1/repos/{OWNER}/{REPO}/pulls",
        params={"state": "closed", "sort": "recentupdate", "limit": 50},
        headers={"Authorization": f"token {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    out = []
    for pr in resp.json():
        if not pr.get("merged"):
            continue
        if pr.get("base", {}).get("ref") != "integration":
            continue
        merged_at = datetime.datetime.fromisoformat(
            pr["merged_at"].replace("Z", "+00:00")
        )
        if merged_at >= since:
            out.append(pr)
    return out


def pr_diff(token: str, number: int) -> str:
    resp = requests.get(
        f"{FORGEJO}/api/v1/repos/{OWNER}/{REPO}/pulls/{number}.diff",
        headers={"Authorization": f"token {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def finding_actions(pr: dict, result: dict) -> tuple[str | None, str | None]:
    """(lesson text, issue title) for one audited merge; (None, None) = clean."""
    if result["verdict"] == "PASS":
        return None, None
    reasons = "; ".join(
        f"[{f['lens']}] {f['notes'][-200:]}" for f in result["blocking"]
    )
    lesson = (
        f"Morning audit flagged merged PR #{pr['number']} "
        f"({pr.get('title', '')!r}): {reasons}"
    )
    issue = f"[morning-audit] PR #{pr['number']} needs human review"
    return lesson, issue


def open_issue(token: str, title: str, body: str) -> None:
    requests.post(
        f"{FORGEJO}/api/v1/repos/{OWNER}/{REPO}/issues",
        headers={"Authorization": f"token {token}"},
        json={"title": title, "body": body},
        timeout=30,
    ).raise_for_status()


async def audit(since_hours: int) -> int:
    from agent.openbao import lease_secret

    token = lease_secret("forgejo")
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        hours=since_hours
    )
    prs = merged_integration_prs(token, since)
    log.info("auditing %d merge(s) since %s", len(prs), since.isoformat())

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    flagged = 0
    for pr in prs:
        diff = pr_diff(token, pr["number"])
        bundle = f"# Task spec\n{pr.get('title', '')}\n\n# Diff under review\n{diff}"
        result = verify(bundle)
        lesson, issue_title = finding_actions(pr, result)
        if lesson is None:
            log.info("PR #%s: clean", pr["number"])
            continue
        flagged += 1
        # lessons need a task row; audits attach to the PR's task when the
        # branch encodes it, else store task-less directly.
        task_row = await pool.fetchval(
            "SELECT task_id FROM tasks WHERE branch = $1",
            pr.get("head", {}).get("ref", ""),
        )
        if task_row:
            await write_lesson(pool, str(task_row), lesson, "review",
                               tag="morning-audit")
        else:
            import uuid as _uuid

            from agent.lessons import embed, to_pgvector

            await pool.execute(
                "INSERT INTO lessons(lesson_id, text, tag, embedding) "
                "VALUES($1, $2, 'morning-audit', $3)",
                _uuid.uuid4(),
                lesson,
                to_pgvector(embed(lesson)),
            )
        open_issue(token, issue_title, lesson)
        log.warning("PR #%s flagged -> lesson + needs-human issue", pr["number"])
    await pool.close()
    print(f"morning audit: {len(prs)} merge(s) audited, {flagged} flagged")
    return 0


def main() -> None:
    logging.basicConfig(level="INFO", format="%(name)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since-hours", type=int, default=24)
    sys.exit(asyncio.run(audit(p.parse_args().since_hours)))


if __name__ == "__main__":
    main()
