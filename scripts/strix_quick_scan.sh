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
#                    'strix', no issues opened; needs the swarm's Postgres);
#                    0 = scan and print the findings only
#   STRIX_FAIL_ON    unset (default): the lane is advisory — a completed scan
#                    is exit 0 whatever it found. Set to critical|high|medium|
#                    low|info to pass `--fail-on` and exit 2 when a finding
#                    is at or above it (how the workflow becomes blocking).
# Extra arguments go to strix (e.g. --scope-mode diff --diff-base origin/integration).
#
# Needs a Docker-compatible runtime on this host: the CLI runs the target in
# its sandbox container.
#
# Exit codes of strix-agent 1.7.0 in headless mode (strix/interface/main.py):
# 0 = completed, nothing at or above the threshold; 2 = completed with
# findings (any finding when no --fail-on) — but also a usage error; anything
# else = the scan did not complete. A report, strix_runs/<run>/vulnerabilities.json,
# is written only when there are findings. So: a new report decides whether
# an exit 2 was a scan with findings.
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

mkdir -p strix_runs
reports() { find strix_runs -mindepth 2 -maxdepth 2 -name vulnerabilities.json | sort; }
before="$(reports)"
gate=()
if [ -n "${STRIX_FAIL_ON:-}" ]; then gate=(--fail-on "$STRIX_FAIL_ON"); fi

rc=0
"$ENV_DIR/bin/strix" -n -t "$TARGET" -m "$MODE" ${gate[@]+"${gate[@]}"} "$@" || rc=$?

# the report of the run just made: one that was not there before the scan
report="$(comm -13 <(printf '%s\n' "$before") <(reports) | head -1)"

if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  echo "strix: the scan did not complete (exit $rc)" >&2
  exit "$rc"
fi
if [ -z "$report" ]; then
  if [ "$rc" -eq 2 ]; then
    echo "strix: exit 2 and no report — a usage error, not a scan" >&2
    exit 2
  fi
  echo "strix: scan completed, no findings"
  exit 0
fi

echo "strix: scan completed with findings — $report"
python3 - "$report" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    findings = json.load(f)
for item in findings if isinstance(findings, list) else []:
    print(f"  [{item.get('severity', '?')}] {item.get('title', '(untitled)')}")
PY
if [ "${STRIX_ROUTE:-1}" = 1 ]; then
  uv run --no-sync python scripts/strix_findings.py --report "$report" --no-issues
fi
if [ -n "${STRIX_FAIL_ON:-}" ] && [ "$rc" -eq 2 ]; then
  echo "strix: a finding is at or above STRIX_FAIL_ON=$STRIX_FAIL_ON" >&2
  exit 2
fi
exit 0
