"""Strix findings -> lessons + needs-human issues (v4.2 T8.3).

Reads Strix's JSON findings report (from a `strix -n ... --output json`
run on the security lane), and for each VALIDATED finding (Strix ships a
PoC, so validated = has a proof/reproduction):

  - writes a lesson with tag='strix' (the T0.2 retrieval flywheel), so
    agents learn the vulnerable pattern;
  - for a merged-then-flagged issue, opens a `needs-human` Forgejo issue
    via the same path scripts/morning_audit.py uses.

Mirrors scripts/morning_audit.py exactly (tag-lesson + needs-human issue),
so the DLQ/triage surface is consistent. Strix itself runs on a dedicated
lane against the swarm's own repos only (conventions/security-scope.md).

    python scripts/strix_findings.py --report strix-report.json
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
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

log = logging.getLogger("strix-findings")

FORGEJO = os.environ.get("FORGEJO_URL", "http://localhost:3000")
OWNER = os.environ.get("FORGEJO_OWNER", "swarm")
REPO = os.environ.get("FORGEJO_REPO", "sandbox-repo")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)


def validated_findings(report: dict) -> list[dict]:
    """Findings Strix validated with a PoC/reproduction (not speculative)."""
    items = report.get("findings", report.get("vulnerabilities", []))
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        validated = (
            it.get("validated")
            or it.get("poc")
            or it.get("proof_of_concept")
            or str(it.get("status", "")).lower() in {"confirmed", "validated"}
        )
        if validated:
            out.append(it)
    return out


def finding_lesson(it: dict) -> str:
    title = it.get("title") or it.get("name") or it.get("type") or "vulnerability"
    sev = it.get("severity", it.get("priority", "unknown"))
    loc = it.get("location") or it.get("file") or it.get("url") or ""
    poc = str(it.get("poc") or it.get("proof_of_concept") or "")[:400]
    return (
        f"Strix validated {sev} vulnerability: {title} @ {loc}. "
        f"PoC: {poc}. Fix the pattern; add a regression test."
    )


def open_needs_human_issue(token: str, it: dict, lesson: str) -> None:
    title = f"[strix] {it.get('title') or it.get('name') or 'validated finding'}"
    requests.post(
        f"{FORGEJO}/api/v1/repos/{OWNER}/{REPO}/issues",
        headers={"Authorization": f"token {token}"},
        json={"title": title, "body": lesson, "labels": ["needs-human"]},
        timeout=30,
    ).raise_for_status()


async def process(report: dict, open_issues: bool) -> int:
    from agent.lessons import embed, to_pgvector

    findings = validated_findings(report)
    if not findings:
        print("strix findings: none validated")
        return 0

    token = None
    if open_issues:
        from agent.openbao import lease_secret

        try:
            token = lease_secret("forgejo")
        except Exception as err:  # noqa: BLE001
            log.warning("no Forgejo token (%s); skipping issues", err)

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    written = 0
    for it in findings:
        lesson = finding_lesson(it)
        if await pool.fetchval("SELECT 1 FROM lessons WHERE text = $1", lesson):
            continue
        await pool.execute(
            "INSERT INTO lessons(lesson_id, text, tag, embedding) "
            "VALUES($1, $2, 'strix', $3)",
            uuid.uuid4(),
            lesson,
            to_pgvector(embed(lesson)),
        )
        written += 1
        if token:
            open_needs_human_issue(token, it, lesson)
        log.info("strix finding -> lesson%s", " + issue" if token else "")
    await pool.close()
    print(f"strix findings: {len(findings)} validated, {written} new lesson(s)")
    return 0


def main() -> None:
    logging.basicConfig(level="INFO", format="%(name)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report", required=True, help="Strix JSON findings report")
    p.add_argument("--no-issues", action="store_true", help="lessons only")
    args = p.parse_args()
    with open(args.report) as f:
        report = json.load(f)
    sys.exit(asyncio.run(process(report, not args.no_issues)))


if __name__ == "__main__":
    main()
