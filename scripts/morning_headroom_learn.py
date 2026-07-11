"""headroom learn -> Morning Operator Routine + lessons (v4.1 T1.5).

Nightly session logs go through `headroom learn` (dry-run by default);
proposed context corrections are opened as a feature/* PR against the
swarm repo (NEVER a direct write — Hard Rule 4 + review gates), and
accepted corrections are mirrored into lessons with tag='headroom-learn'
(the T0.2 flywheel).

    python scripts/morning_headroom_learn.py            # dry-run: emit diff
    python scripts/morning_headroom_learn.py --pr       # open the feature/* PR
    python scripts/morning_headroom_learn.py --mirror-accepted  # merged -> lessons
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os
import subprocess
import sys
import tempfile

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

log = logging.getLogger("headroom-learn")

FORGEJO = os.environ.get("FORGEJO_URL", "http://localhost:3000")
OWNER = os.environ.get("FORGEJO_OWNER", "swarm")
REPO = os.environ.get("SWARM_REPO", "team-swarm")  # the swarm's own repo
BOT = os.environ.get("FORGEJO_BOT_USER", "swarm-bot")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)
TARGET_FILE = "conventions/learned.md"


def run_learn(dry_run: bool = True, fixture_log: str | None = None) -> str:
    """Run `headroom learn` and return its proposed corrections text.

    fixture_log substitutes a recorded session log for tests.
    """
    if fixture_log is not None:
        return parse_learn_output(fixture_log)
    cmd = ["headroom", "learn", "--target", TARGET_FILE]
    if dry_run:
        cmd.append("--dry-run")
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800
        ).stdout
    except FileNotFoundError:
        raise SystemExit(
            "headroom CLI not installed: pip install -r requirements-elevation.txt"
        ) from None
    return parse_learn_output(out)


def parse_learn_output(output: str) -> str:
    """Extract the proposed-correction lines from learn output."""
    keep: list[str] = []
    capture = False
    for line in output.splitlines():
        low = line.strip().lower()
        if low.startswith(("## ", "- ", "* ")) or capture:
            capture = True
            keep.append(line.rstrip())
        elif "recommendation" in low or "correction" in low:
            capture = True
    return "\n".join(keep).strip()


def build_pr_body(corrections: str, date: str) -> str:
    return (
        f"## headroom learn — {date}\n\n"
        "Proposed context corrections from last night's session logs "
        "(`headroom learn`, dry-run reviewed).\n\n"
        "Per the Morning Operator Routine these NEVER write directly to "
        "agent context: this PR goes through the normal review gates, and "
        "accepted corrections are mirrored into "
        "`lessons(tag='headroom-learn')`.\n\n---\n\n"
        f"{corrections}\n"
    )


def open_learn_pr(corrections: str, token: str) -> str:
    """Clone the swarm repo, append corrections, push feature/*, open a PR."""
    date = datetime.date.today().isoformat()
    branch = f"feature/headroom-learn-{date}"
    remote = (
        f"{FORGEJO.split('://', 1)[0]}://{BOT}:{token}@"
        f"{FORGEJO.split('://', 1)[1]}/{OWNER}/{REPO}.git"
    )
    with tempfile.TemporaryDirectory() as workdir:
        repo = os.path.join(workdir, "repo")
        subprocess.run(["git", "clone", "--depth", "1", remote, repo], check=True)
        subprocess.run(["git", "checkout", "-b", branch], cwd=repo, check=True)
        target = os.path.join(repo, TARGET_FILE)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a") as f:
            f.write(f"\n\n## {date} (headroom learn)\n\n{corrections}\n")
        subprocess.run(["git", "add", TARGET_FILE], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", f"user.name={BOT}", "-c", f"user.email={BOT}@swarm",
             "commit", "-m", f"headroom learn corrections {date}"],
            cwd=repo, check=True,
        )
        subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo, check=True)
    resp = requests.post(
        f"{FORGEJO}/api/v1/repos/{OWNER}/{REPO}/pulls",
        headers={"Authorization": f"token {token}"},
        json={
            "title": f"headroom learn corrections — {date}",
            "head": branch,
            "base": "integration",
            "body": build_pr_body(corrections, date),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return branch


async def mirror_accepted(token: str) -> int:
    """Merged learn-PR corrections -> lessons(tag='headroom-learn')."""
    import uuid

    import asyncpg

    from agent.lessons import embed, to_pgvector

    resp = requests.get(
        f"{FORGEJO}/api/v1/repos/{OWNER}/{REPO}/pulls",
        params={"state": "closed", "limit": 20},
        headers={"Authorization": f"token {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    mirrored = 0
    for pr in resp.json():
        if not pr.get("merged") or not pr.get("head", {}).get(
            "ref", ""
        ).startswith("feature/headroom-learn-"):
            continue
        text = f"[headroom-learn] accepted: {pr.get('title')}\n{pr.get('body', '')[:1500]}"
        if await pool.fetchval("SELECT 1 FROM lessons WHERE text = $1", text):
            continue
        await pool.execute(
            "INSERT INTO lessons(lesson_id, text, tag, embedding) "
            "VALUES($1, $2, 'headroom-learn', $3)",
            uuid.uuid4(),
            text,
            to_pgvector(embed(text)),
        )
        mirrored += 1
    await pool.close()
    print(f"mirrored {mirrored} accepted correction set(s) into lessons")
    return 0


def main() -> None:
    logging.basicConfig(level="INFO", format="%(name)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pr", action="store_true", help="open the feature/* PR")
    p.add_argument("--mirror-accepted", action="store_true")
    p.add_argument("--fixture-log", help="use a recorded session log (tests)")
    args = p.parse_args()

    from agent.openbao import lease_secret

    if args.mirror_accepted:
        sys.exit(asyncio.run(mirror_accepted(lease_secret("forgejo"))))

    fixture = open(args.fixture_log).read() if args.fixture_log else None
    corrections = run_learn(dry_run=True, fixture_log=fixture)
    if not corrections:
        print("headroom learn: no corrections proposed")
        return
    print("--- proposed corrections (dry-run) ---")
    print(corrections)
    if args.pr:
        branch = open_learn_pr(corrections, lease_secret("forgejo"))
        print(f"opened PR from {branch} (review gates apply)")


if __name__ == "__main__":
    main()
