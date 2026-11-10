"""Codex review as a second, heterogeneous reviewer (v4.2 T7.4).

Runs `codex exec --json review --base <branch>` over a diff and maps its
prioritized findings into the same finding shape agent/verifier.py uses,
so the orchestrator's aggregate() treats Codex as one more reviewer lens.
Verified finding: heterogeneous reviewers (a different engine/model) beat
homogeneous ones — Codex-on-Ornith complements the Ornith verifier lenses.

Codex runs self-hosted via --oss on the review lane; it is invoked as an
external tool (like aider), never imported. The lane fails closed (review
R7): an absent `codex` binary, a non-zero exit, or output with no parseable
finding list is a single ERROR finding — never a PASS (aggregate blocks).

    python -m agent.codex_review --base integration            # in a repo
    python -m agent.codex_review --base integration --json-out findings.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys

log = logging.getLogger("codex-review")

CODEX_REVIEW_FLAGS = os.environ.get("CODEX_REVIEW_FLAGS", "--oss")
REVIEW_TIMEOUT_S = int(os.environ.get("CODEX_REVIEW_TIMEOUT_S", "600"))


def codex_available() -> bool:
    return shutil.which("codex") is not None


def run_codex_review(base: str, cwd: str = ".") -> str:
    """Invoke `codex exec <flags> --json review --base <base>`; returns raw stdout.

    In the pinned CLI (rust-v0.167.0) `--json` and `--oss` are options of
    `codex exec`, and `review --base` is its subcommand; the top-level
    `codex review` takes neither. A non-zero exit raises: a reviewer that
    crashed has reviewed nothing.
    """
    flags = shlex.split(CODEX_REVIEW_FLAGS)
    cmd = ["codex", "exec", *flags, "--json", "review", "--base", base]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=REVIEW_TIMEOUT_S)
    if proc.returncode != 0:
        raise subprocess.SubprocessError(
            f"codex review exited {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}"
        )
    return proc.stdout


def parse_findings(raw: str) -> list[dict]:
    """Map Codex JSON review output to verifier finding dicts.

    Codex emits prioritized findings; any high/critical priority (or an
    explicit block) yields VERDICT: BLOCK. Shape matches verifier.py:
    {"lens": "codex", "verdict": PASS|BLOCK|ERROR, "notes": ...}.
    Output that carries no JSON at all is ERROR, not PASS: only an explicit,
    parseable finding list (possibly empty) says "nothing found".
    """
    parsed = True
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Codex may stream one JSON object per line; try line mode.
        data = []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    data.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        parsed = bool(data)
    if not parsed:
        return [
            {
                "lens": "codex",
                "verdict": "ERROR",
                "notes": "codex review produced no parseable JSON findings",
            }
        ]

    items = data.get("findings", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []
    blocking = [
        it
        for it in items
        if isinstance(it, dict)
        and str(it.get("priority", it.get("severity", ""))).lower()
        in {"high", "critical", "blocker", "error"}
    ]
    verdict = "BLOCK" if blocking else "PASS"
    notes = (
        "; ".join(str(it.get("title") or it.get("message") or it)[:200] for it in items[:10])
        or "no findings"
    )
    return [{"lens": "codex", "verdict": verdict, "notes": notes[:1500]}]


def review(base: str, cwd: str = ".") -> list[dict]:
    """One Codex-review finding for aggregate(); ERROR if codex is absent."""
    if not codex_available():
        log.warning("codex binary not installed; skipping Codex review lane")
        return [
            {
                "lens": "codex",
                "verdict": "ERROR",
                "notes": "codex CLI not installed (T7.4 optional lane)",
            }
        ]
    try:
        return parse_findings(run_codex_review(base, cwd))
    except (subprocess.SubprocessError, OSError) as err:
        return [{"lens": "codex", "verdict": "ERROR", "notes": str(err)[:200]}]


def main() -> None:
    logging.basicConfig(level="INFO")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="integration")
    p.add_argument("--cwd", default=".")
    p.add_argument("--json-out", help="write findings JSON to this path")
    args = p.parse_args()
    findings = review(args.base, args.cwd)
    out = json.dumps(findings, indent=2)
    if args.json_out:
        with open(args.json_out, "w") as f:
            f.write(out)
    print(out)
    # exit non-zero if any lens blocks (feeds CI / aggregate)
    sys.exit(1 if any(f["verdict"] != "PASS" for f in findings) else 0)


if __name__ == "__main__":
    main()
