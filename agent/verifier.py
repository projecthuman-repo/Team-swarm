"""Independent verifier with no shared author context (v4.1 T2.3).

The verified finding: author-model verification preserves author biases —
so the verifier never sees the author's conversation. It reviews ONLY the
diff + task spec (delivered via Headroom SharedContext, T1.7) from a
fresh perspective.

Tasks labelled swarm-hard fan out three parallel lenses — Architect /
Security / QA — whose findings are aggregated; any BLOCK verdict blocks.

    python -m agent.verifier <task_id>            # bundle from SharedContext
    python -m agent.verifier --diff d.patch --spec s.md
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os

import requests

from agent.shared_context import get_handoff

log = logging.getLogger("verifier")

OPENAI_BASE = os.environ.get(
    "VERIFIER_API_BASE",
    os.environ.get("OPENAI_API_BASE", "http://localhost:11434/v1"),
)
MODEL = os.environ.get("VERIFIER_MODEL", "Ornith-1.0-9B")

LENSES = {
    "architect": (
        "You are an independent ARCHITECT reviewer. Judge the diff for "
        "design coherence, unnecessary complexity, and surgical scope "
        "(only files the task names)."
    ),
    "security": (
        "You are an independent SECURITY reviewer. Judge the diff for "
        "injection, secret handling, path traversal, and privilege issues."
    ),
    "qa": (
        "You are an independent QA reviewer. Judge the diff for missing "
        "tests, unverified success criteria, and silent behavior changes."
    ),
}
VERDICT_INSTRUCTIONS = (
    'End with exactly one line: VERDICT: PASS or VERDICT: BLOCK — <reason>. '
    "You have ONLY the spec and diff below; do not assume any other context."
)


def review_with_lens(lens: str, bundle: str) -> dict:
    """One lens review over the hand-off bundle. Returns {lens, verdict, notes}."""
    prompt = f"{LENSES[lens]}\n{VERDICT_INSTRUCTIONS}\n\n{bundle}"
    try:
        resp = requests.post(
            f"{OPENAI_BASE.rstrip('/')}/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            timeout=600,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
    except requests.RequestException as err:
        return {"lens": lens, "verdict": "ERROR", "notes": str(err)}
    return {"lens": lens, "verdict": parse_verdict(text), "notes": text[-1500:]}


def parse_verdict(text: str) -> str:
    for line in reversed(text.strip().splitlines()):
        if line.strip().upper().startswith("VERDICT:"):
            return "PASS" if "PASS" in line.upper() else "BLOCK"
    return "BLOCK"  # fail-closed: no explicit verdict = block


def aggregate(findings: list[dict]) -> dict:
    """Any BLOCK blocks; ERRORs block too (fail-closed)."""
    blocking = [f for f in findings if f["verdict"] != "PASS"]
    return {
        "verdict": "PASS" if not blocking else "BLOCK",
        "findings": findings,
        "blocking": blocking,
    }


# v4.2 T7.8 — rubric grading mirrors Claude Code's Performance-Outcomes
# pattern: a rubric grader scores the result and forces exactly ONE
# revision on a miss, then escalates rather than looping forever.
RUBRIC = os.environ.get(
    "VERIFIER_RUBRIC",
    "1. The diff addresses the task spec. "
    "2. It includes or updates tests. "
    "3. Scope is surgical (only files the task names). "
    "4. No secrets, debug prints, or TODOs left behind.",
)
RUBRIC_PASS_THRESHOLD = float(os.environ.get("RUBRIC_PASS_THRESHOLD", "0.75"))


def grade_rubric(bundle: str) -> dict:
    """Score the bundle against the rubric. Returns {score, verdict, notes}.

    verdict MISS (< threshold) triggers one forced revision upstream; a
    second MISS escalates (T7.8), never an unbounded loop.
    """
    prompt = (
        "You are a rubric grader. Score the diff against each numbered "
        "criterion as pass(1)/fail(0). Reply with the fraction passed as "
        "'SCORE: N/M' on the last line.\n\nRubric:\n"
        f"{RUBRIC}\n\n{bundle}"
    )
    try:
        resp = requests.post(
            f"{OPENAI_BASE.rstrip('/')}/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
            timeout=600,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
    except requests.RequestException as err:
        return {"score": 0.0, "verdict": "MISS", "notes": str(err)}
    score = parse_rubric_score(text)
    return {
        "score": score,
        "verdict": "HIT" if score >= RUBRIC_PASS_THRESHOLD else "MISS",
        "notes": text[-800:],
    }


def parse_rubric_score(text: str) -> float:
    """Parse 'SCORE: N/M' (bottom-up); fail-closed to 0.0."""
    import re

    for line in reversed(text.strip().splitlines()):
        m = re.search(r"SCORE:\s*(\d+)\s*/\s*(\d+)", line, re.I)
        if m:
            num, den = int(m.group(1)), int(m.group(2))
            return num / den if den else 0.0
    return 0.0


def verify(bundle: str, lenses: list[str] | None = None) -> dict:
    lenses = lenses or list(LENSES)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(lenses)) as ex:
        findings = list(ex.map(lambda x: review_with_lens(x, bundle), lenses))
    return aggregate(findings)


def main() -> None:
    logging.basicConfig(level="INFO")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("task_id", nargs="?", help="fetch bundle from SharedContext")
    p.add_argument("--diff", help="path to a diff file")
    p.add_argument("--spec", help="path to a spec file")
    p.add_argument("--lens", action="append", choices=list(LENSES))
    args = p.parse_args()

    if args.task_id:
        bundle = get_handoff(args.task_id)
        if bundle is None:
            raise SystemExit(f"no hand-off bundle for task {args.task_id}")
    else:
        diff = open(args.diff).read() if args.diff else ""
        spec = open(args.spec).read() if args.spec else ""
        bundle = f"# Task spec\n{spec}\n\n# Diff under review\n{diff}"

    result = verify(bundle, args.lens)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
