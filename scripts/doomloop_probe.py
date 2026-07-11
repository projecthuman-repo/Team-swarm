"""Doom-loop baseline measurement (v4.2 T9.2).

Reasoning-heavy coders like Ornith can fall into "doom loops" — runaway
repetition that burns the token budget (the reason=snowball failure mode
T5.3 guards at runtime). Antidoom (WS9) attacks it at the model level; to
know whether an anti-doom checkpoint helps (T9.4), we first measure the
current rate.

This probe detects repetition spans in agent transcripts and reports a
doom-loop rate, correlated with reason=snowball DLQ hits. The repetition
detector is pure logic (unit-tested); the DB/transcript sources are
optional.

    python scripts/doomloop_probe.py --transcripts dir/         # from files
    python scripts/doomloop_probe.py --snowball-rate            # DLQ correlate
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import logging
import os

log = logging.getLogger("doomloop-probe")

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:dev@localhost:5432/swarm"
)
# A doom loop = the same normalized line repeated >= N times in a row, or a
# repeated n-gram block covering a large fraction of the tail.
MIN_CONSECUTIVE = int(os.environ.get("DOOMLOOP_MIN_CONSECUTIVE", "6"))
NGRAM = int(os.environ.get("DOOMLOOP_NGRAM", "12"))
NGRAM_REPEAT = int(os.environ.get("DOOMLOOP_NGRAM_REPEAT", "4"))


def _norm(line: str) -> str:
    return " ".join(line.split()).lower()


def max_consecutive_repeat(lines: list[str]) -> int:
    """Longest run of an identical normalized non-empty line."""
    best = run = 0
    prev = None
    for raw in lines:
        line = _norm(raw)
        if not line:
            prev = None
            run = 0
            continue
        if line == prev:
            run += 1
        else:
            run = 1
            prev = line
        best = max(best, run)
    return best


def max_ngram_repeat(tokens: list[str], n: int) -> int:
    """Max occurrences of any repeated n-gram (token window)."""
    if len(tokens) < n:
        return 0
    counts: dict[tuple, int] = {}
    best = 0
    for i in range(len(tokens) - n + 1):
        gram = tuple(tokens[i : i + n])
        counts[gram] = counts.get(gram, 0) + 1
        best = max(best, counts[gram])
    return best


def is_doom_loop(text: str) -> bool:
    """True if the transcript shows runaway repetition (a doom loop)."""
    lines = text.splitlines()
    if max_consecutive_repeat(lines) >= MIN_CONSECUTIVE:
        return True
    tokens = _norm(text).split()
    return max_ngram_repeat(tokens, NGRAM) >= NGRAM_REPEAT


def probe_transcripts(paths: list[str]) -> dict:
    """Doom-loop rate over a set of transcript files."""
    total = looped = 0
    for path in paths:
        try:
            with open(path, errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        total += 1
        if is_doom_loop(text):
            looped += 1
    return {
        "transcripts": total,
        "doom_loops": looped,
        "doom_loop_rate": (looped / total) if total else 0.0,
    }


async def snowball_rate() -> dict:
    """Fraction of failed tasks tagged reason=snowball (T5.3 correlate)."""
    import asyncpg

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    row = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE status='failed') AS failed,
               count(*) FILTER (
                 WHERE status='failed' AND task_id IN (
                   SELECT task_id FROM lessons WHERE text ILIKE '%snowball%'
                 )) AS snowball
        FROM tasks
        """
    )
    await pool.close()
    failed = row["failed"] or 0
    return {
        "failed_tasks": failed,
        "snowball_failures": row["snowball"] or 0,
        "snowball_rate": (row["snowball"] / failed) if failed else 0.0,
    }


def main() -> None:
    logging.basicConfig(level="INFO")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--transcripts", help="dir of *.txt/*.log agent transcripts")
    p.add_argument("--snowball-rate", action="store_true")
    args = p.parse_args()

    if args.transcripts:
        paths = glob.glob(os.path.join(args.transcripts, "**", "*"), recursive=True)
        paths = [x for x in paths if x.endswith((".txt", ".log", ".jsonl"))]
        result = probe_transcripts(paths)
        print(f"doom-loop rate: {result['doom_loops']}/{result['transcripts']} "
              f"= {result['doom_loop_rate']:.1%}")
    if args.snowball_rate:
        result = asyncio.run(snowball_rate())
        print(f"snowball failures: {result['snowball_failures']}/"
              f"{result['failed_tasks']} = {result['snowball_rate']:.1%}")
    if not args.transcripts and not args.snowball_rate:
        p.error("give --transcripts DIR and/or --snowball-rate")


if __name__ == "__main__":
    main()
