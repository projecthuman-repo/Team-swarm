"""Verification-aware planning (v4.1 T3.1) + decomposition policy (T3.2).

Policy first (T3.2, verified finding): a single agent is the DEFAULT —
orchestration multiplies token cost and can merely rival a strong single
model. Fan-out happens ONLY for tasks labelled swarm-hard (or explicitly
marked decomposable). Triage labels drive routing.

When a task does decompose, the planner emits subtasks EACH PAIRED WITH A
VERIFICATION COMMAND (test command / ruff / verifier review). Subtasks
carry parent_task_id + verify_cmd (append-only proto fields) and flow
through the existing outbox -> JetStream path — the coordinator replans
on verify-fail.
"""

from __future__ import annotations

import logging
import os
import re
import uuid

import asyncpg

log = logging.getLogger("planner")

DEFAULT_BUDGET = int(os.environ.get("DEFAULT_BUDGET_TOKENS", "200000"))
ROLES = {"backend", "frontend", "review", "triage"}
DEFAULT_VERIFY_CMD = os.environ.get("PLANNER_DEFAULT_VERIFY", "pytest -q")


def should_decompose(title: str, labels: list[str] | None = None) -> bool:
    """T3.2 — single agent unless the work is marked swarm-hard."""
    labels = labels or []
    if "swarm-hard" in labels:
        return True
    return "[swarm-hard]" in (title or "").lower()


def parse_plan(text: str) -> list[dict]:
    """Parse planner output lines into subtask dicts.

    Format (one per line):  <role>: <title> :: verify: <command>
    Lines without a verify command get the default (tests must pass) —
    every subtask is verification-paired, by construction.
    """
    subtasks = []
    for line in text.splitlines():
        m = re.match(r"\s*[-*\d.]*\s*(\w+)\s*:\s*(.+)", line.strip())
        if not m or m.group(1).lower() not in ROLES:
            continue
        role = m.group(1).lower()
        rest = m.group(2)
        verify = DEFAULT_VERIFY_CMD
        if "::" in rest:
            rest, _, verify_part = rest.partition("::")
            verify = verify_part.replace("verify:", "").strip() or DEFAULT_VERIFY_CMD
        subtasks.append(
            {"role": role, "title": rest.strip(), "verify_cmd": verify}
        )
    return subtasks


def plan_subtasks(title: str, spec: str = "") -> list[dict]:
    """Produce verification-paired subtasks for a decomposable task.

    Uses the local model when reachable; falls back to a deterministic
    single-subtask plan so the DAG always has something verifiable.
    """
    prompt = (
        "Decompose this task into 2-5 subtasks, one per line, format\n"
        "<role>: <title> :: verify: <shell command>\n"
        f"Roles: {sorted(ROLES)}. Task: {title}\n{spec}"
    )
    try:
        import requests

        base = os.environ.get("OPENAI_API_BASE", "http://localhost:11434/v1")
        resp = requests.post(
            f"{base.rstrip('/')}/chat/completions",
            json={
                "model": os.environ.get("PLANNER_MODEL", "Ornith-1.0-9B"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            timeout=300,
        )
        resp.raise_for_status()
        parsed = parse_plan(resp.json()["choices"][0]["message"]["content"])
        if parsed:
            return parsed
    except Exception as err:
        log.warning("LLM planning unavailable (%s); deterministic fallback", err)
    return [{"role": "backend", "title": title, "verify_cmd": DEFAULT_VERIFY_CMD}]


async def emit_subtasks(
    pool: asyncpg.Pool, parent_task_id: str, subtasks: list[dict]
) -> list[str]:
    """Write each subtask (tasks row + outbox row) in ONE tx apiece.

    Nothing reaches JetStream except via the relay (Hard Rule 2).
    """
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen"))
    from swarm.v1 import task_pb2

    ids = []
    for sub in subtasks:
        tid = str(uuid.uuid4())
        task = task_pb2.Task(
            task_id=tid,
            role=sub["role"],
            title=sub["title"],
            branch=f"feature/{tid}",
            budget_tokens=DEFAULT_BUDGET,
            parent_task_id=parent_task_id,
            verify_cmd=sub.get("verify_cmd", ""),
            tier=sub.get("tier", ""),
        )
        subject = (
            f"swarm.tasks.{sub['role']}.{sub['tier']}"
            if sub.get("tier")
            else f"swarm.tasks.{sub['role']}"
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO tasks(task_id, role, status, title, branch, "
                    "budget_tokens) VALUES($1, $2, 'pending', $3, $4, $5)",
                    uuid.UUID(tid),
                    sub["role"],
                    sub["title"],
                    f"feature/{tid}",
                    DEFAULT_BUDGET,
                )
                await conn.execute(
                    "INSERT INTO outbox(subject, msg_id, payload) VALUES($1, $2, $3)",
                    subject,
                    tid,
                    task.SerializeToString(),
                )
        ids.append(tid)
        log.info("planned subtask %s (%s, verify: %s)",
                 tid, sub["role"], sub.get("verify_cmd"))
    return ids


async def subtask_states(pool: asyncpg.Pool, subtask_ids: list[str]) -> dict:
    rows = await pool.fetch(
        "SELECT task_id, status FROM tasks WHERE task_id = ANY($1::uuid[])",
        [uuid.UUID(t) for t in subtask_ids],
    )
    return {str(r["task_id"]): r["status"] for r in rows}
