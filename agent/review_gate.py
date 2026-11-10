"""Pre-push review step (review R11; v4.2 T7.4 + T7.8) — off by default.

v4.2 shipped the independent verifier, the rubric grader and the Codex
reviewer as entry points with no caller in the task flow. This module is
the one point in the flow that consumes the independent verifier's verdict.
The worker runs it on a green change, before the push, and only when a
flag asks for it:

  RUBRIC_ENABLED=1   every task is graded against the rubric
                     (verifier.grade_rubric). A first MISS forces exactly
                     one revision carrying the grader's notes; a second
                     MISS escalates when escalation is enabled for the task
                     and otherwise takes the needs-human path (DLQ). The
                     counter is tasks.rubric_misses, so a redelivery cannot
                     buy a third round.
  CODEX_REVIEW=1     swarm-hard reviews get codex_review.review as a fourth
                     reviewer beside Architect, Security and QA. Its ERROR
                     (CLI unavailable, output unparseable) is recorded and
                     does not block unless CODEX_REVIEW_REQUIRED=1.

When the step runs, a swarm-hard task gets the three-lens review (a single
agent is the default for everything else, T3.2); any BLOCK — and any lens
ERROR, fail-closed — means the change is not pushed.

The reviewers see only the hand-off bundle (spec + diff, T2.3), never the
author's conversation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass

log = logging.getLogger("review-gate")

RUBRIC_ENABLED = os.environ.get("RUBRIC_ENABLED", "0") == "1"
CODEX_REVIEW = os.environ.get("CODEX_REVIEW", "0") == "1"
CODEX_REVIEW_REQUIRED = os.environ.get("CODEX_REVIEW_REQUIRED", "0") == "1"


def wanted(hard: bool) -> bool:
    """Does any flag ask for the review step on this task?"""
    return RUBRIC_ENABLED or (CODEX_REVIEW and hard)


def review(bundle: str, hard: bool, base: str = "", cwd: str = ".") -> dict:
    """Consume the independent verifier's verdict for one bundle.

    Returns {verdict, findings, blocking, rubric}: the aggregate over the
    lenses (and Codex) for a swarm-hard task, PASS with no findings
    otherwise; `rubric` is grade_rubric's result, or None when it is off.
    """
    from agent import verifier

    result: dict = {"verdict": "PASS", "findings": [], "blocking": [], "rubric": None}
    if hard:
        findings = verifier.verify(bundle)["findings"]
        if CODEX_REVIEW:
            from agent import codex_review

            findings = findings + codex_review.review(base, cwd)
        advisory = () if CODEX_REVIEW_REQUIRED else ("codex",)
        result.update(verifier.aggregate(findings, advisory_errors=advisory))
    if RUBRIC_ENABLED:
        result["rubric"] = verifier.grade_rubric(bundle)
    return result


@dataclass
class Verdict:
    action: str  # push | revise | block | escalate
    reason: str = ""
    notes: str = ""


class ReviewGate:
    """The review step for one task. `judge` is called once per green change."""

    def __init__(self, pool, task_id: str, hard: bool, can_escalate: bool):
        self.pool = pool
        self.task_id = task_id
        self.hard = hard
        self.can_escalate = can_escalate

    async def judge(self, diff: str, spec: str, base: str, cwd: str) -> Verdict:
        from agent.shared_context import get_handoff, put_handoff

        put_handoff(self.task_id, diff, spec)
        bundle = get_handoff(self.task_id) or f"# Task spec\n{spec}\n\n# Diff under review\n{diff}"
        result = await asyncio.to_thread(review, bundle, self.hard, base, cwd)

        for finding in result["findings"]:
            if finding["verdict"] == "ERROR" and finding not in result["blocking"]:
                log.warning(
                    "task %s: reviewer %s unavailable, recorded, not blocking: %s",
                    self.task_id,
                    finding["lens"],
                    finding["notes"],
                )
        if result["verdict"] != "PASS":
            notes = "\n".join(
                f"[{f['lens']}] {f['verdict']}: {f['notes']}" for f in result["blocking"]
            )
            lenses = ", ".join(f["lens"] for f in result["blocking"])
            return Verdict("block", f"independent review blocked ({lenses})", notes)

        rubric = result["rubric"]
        if rubric is None or rubric["verdict"] != "MISS":
            return Verdict("push")
        misses = await self.pool.fetchval(
            "UPDATE tasks SET rubric_misses = rubric_misses + 1, updated_at = now() "
            "WHERE task_id = $1 RETURNING rubric_misses",
            uuid.UUID(self.task_id),
        )
        notes = f"rubric score {rubric['score']:.2f}\n{rubric['notes']}"
        if misses == 1:
            return Verdict("revise", "rubric MISS", notes)
        reason = "rubric MISS twice (one revision already forced)"
        return Verdict("escalate" if self.can_escalate else "block", reason, notes)
