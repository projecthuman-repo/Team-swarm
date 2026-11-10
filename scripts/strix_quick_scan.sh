#!/usr/bin/env bash
# The Strix security lane (v4.2 Workstream 8; review R10): the Strix CLI from
# the locked `security` dependency group — what upstream ships. Shared by
# `make strix`, `make strix-scan`, the advisory `security` workflow and
# `make verify-integration`.
#
# Strix is offensive tooling: it runs only on the security lane against the
# swarm's own repos (conventions/security-scope.md), never inside an agent
# container, and it is in no image the swarm builds.
#
#   STRIX_SCAN_MODE  quick (default) | standard | deep          -> strix -m
#   STRIX_TARGET     ./ (default): URL, repository or directory -> strix -t
#   STRIX_IMAGE      the sandbox image the CLI starts; pinned to the default
#                    the locked strix-agent declares
#   STRIX_LLM        default openai/swarm-local
#   LLM_API_BASE     default http://localhost:4000/v1 (the LiteLLM gateway, `make litellm`)
#   STRIX_TELEMETRY  false (default) — no usage callout
#   STRIX_ROUTE      1 (default): turn validated findings into lessons (tag
#                    'strix', no issues opened); 0 = scan only
# Extra arguments go to strix (e.g. --scope-mode diff --diff-base origin/integration).
#
# Needs a Docker-compatible runtime on this host: the CLI runs the target in
# its sandbox container. The report lands in strix_runs/<run>/vulnerabilities.json.
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_PYTHON="${UV_PYTHON:-3.12}"
ENV_DIR="${STRIX_ENV:-${TMPDIR:-/tmp}/strix-env}"
MODE="${STRIX_SCAN_MODE:-quick}"
TARGET="${STRIX_TARGET:-./}"
export STRIX_IMAGE="${STRIX_IMAGE:-ghcr.io/usestrix/strix-sandbox:1.3.0}"
export LLM_API_BASE="${LLM_API_BASE:-http://localhost:4000/v1}"
export STRIX_LLM="${STRIX_LLM:-openai/swarm-local}"
export STRIX_TELEMETRY="${STRIX_TELEMETRY:-false}"

UV_PROJECT_ENVIRONMENT="$ENV_DIR" uv sync -q --frozen --no-install-project --only-group security
echo "strix: mode $MODE, target $TARGET, sandbox $STRIX_IMAGE, model $STRIX_LLM at $LLM_API_BASE"
"$ENV_DIR/bin/strix" -n -t "$TARGET" -m "$MODE" "$@"

found=0
for report in strix_runs/*/vulnerabilities.json; do
  [ -s "$report" ] || continue
  found=1
  [ "${STRIX_ROUTE:-1}" = 1 ] || continue
  uv run --no-sync python scripts/strix_findings.py --report "$report" --no-issues
done
[ "$found" = 1 ] || { echo "strix: no vulnerabilities.json was written" >&2; exit 1; }
