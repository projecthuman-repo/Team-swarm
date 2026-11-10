#!/usr/bin/env bash
# The escalation lane (review R11; v4.2 T7.7) — a HOST-RUN worker.
#
#   ROUTE_API_FALLBACK=1 make escalation     consume swarm.tasks.escalation until stopped
#   scripts/escalation_lane.sh --smoke       one real escalation, end to end
#                                            (a `make verify-integration` step)
#
# Never in a sealed agent container and never in an image: the lane needs
# Anthropic egress, and claude-agent-sdk's wheel bundles a CLI under
# commercial terms (conventions/licensing.md). Its environment is the locked
# `agent` group plus the `escalation` extra, in its own directory:
#   uv sync --frozen --group agent --extra escalation
#
# Off by default (INV-027): without ROUTE_API_FALLBACK=1 the worker refuses
# to start and the smoke is skipped (exit 77). The Claude key is an OpenBao
# lease: secret/swarm/claude, field api_key (ESCALATION_SECRET_NAME).
#
# The host reaches the spine on its published loopback ports; the defaults
# below are those.
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_PYTHON="${UV_PYTHON:-3.12}"
ENV_DIR="${ESCALATION_ENV:-${TMPDIR:-/tmp}/escalation-env}"
export FORGEJO_URL="${FORGEJO_URL:-http://localhost:3000}"
export OLLAMA_API_BASE="${OLLAMA_API_BASE:-http://localhost:11434}"

if [ "${1:-}" != "--smoke" ]; then
  UV_PROJECT_ENVIRONMENT="$ENV_DIR" uv sync -q --frozen --no-install-project --group agent --extra escalation
  PATH="$ENV_DIR/bin:$PATH" exec "$ENV_DIR/bin/python" -m agent.escalation_worker "$@"
fi

if [ "${ROUTE_API_FALLBACK:-0}" != 1 ]; then
  echo "SKIP: ROUTE_API_FALLBACK is not 1. The escalation lane is a hosted fallback: opt-in, off by default (INV-027)."
  echo "      To run this step: ROUTE_API_FALLBACK=1 with a Claude key at OpenBao secret/swarm/claude (field api_key)."
  exit 77
fi
UV_PROJECT_ENVIRONMENT="$ENV_DIR" uv sync -q --frozen --no-install-project --group agent --extra escalation
if ! "$ENV_DIR/bin/python" -c "
import os
from agent.openbao import lease_secret
assert lease_secret(os.environ.get('ESCALATION_SECRET_NAME', 'claude'), field='api_key')
" >/dev/null 2>&1; then
  echo "SKIP: ROUTE_API_FALLBACK=1 but the Claude key lease failed (OpenBao secret/swarm/${ESCALATION_SECRET_NAME:-claude}, field api_key)."
  exit 77
fi

# Stage one hand-off exactly as the normal path writes it (task row, failing
# diff, gate output and the outbox event in one transaction), then let the
# lane handle it: SDK call, patch, the two-stage gate, push, PR_OPENED.
PATH="$ENV_DIR/bin:$PATH" FETCH_TIMEOUT_S="${FETCH_TIMEOUT_S:-120}" \
  "$ENV_DIR/bin/python" - <<'PY'
import asyncio
import sys
import uuid

import asyncpg

from agent import escalation, escalation_worker
from agent.agent_worker import DATABASE_URL
from swarm.v1 import task_pb2

TITLE = (
    "[swarm-hard] Add slug.py with slugify(text): lower-case, runs of "
    "non-alphanumerics become one '-', no leading or trailing '-'. Add "
    "tests/test_slug.py covering 'Hello, World!' -> 'hello-world'."
)


async def main() -> int:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    tid = str(uuid.uuid4())
    task = task_pb2.Task(
        task_id=tid, role="backend", title=TITLE, branch=f"feature/{tid}", budget_tokens=1
    )
    await pool.execute(
        "INSERT INTO tasks(task_id, role, status, claimed_by, title, branch, budget_tokens, labels) "
        "VALUES($1, 'backend', 'claimed', 'escalation-smoke', $2, $3, 1, '{swarm-hard}')",
        uuid.UUID(tid), TITLE, task.branch,
    )
    staged = await escalation.hand_off(
        pool, task, "smoke: staged repair-budget failure", "",
        "$ pytest -q\nE   ModuleNotFoundError: No module named 'slug'", "escalation-smoke",
    )
    assert staged, "hand-off was refused"
    print(f"staged escalation for task {tid}; waiting for the relay and the lane")
    code = await escalation_worker.main(once=True)
    row = await pool.fetchrow(
        "SELECT t.status, t.escalated_at, e.outcome FROM tasks t "
        "JOIN escalations e USING (task_id) WHERE t.task_id=$1", uuid.UUID(tid),
    )
    print(f"task {tid}: status={row['status']} outcome={row['outcome']}")
    ok = code == 0 and row["status"] == "done" and row["outcome"] == "pr_opened"
    return 0 if ok and row["escalated_at"] else 1


sys.exit(asyncio.run(main()))
PY
