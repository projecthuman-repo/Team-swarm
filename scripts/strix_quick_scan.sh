#!/usr/bin/env bash
# Strix quick scan of this repository (v4.2 Workstream 8), from the locked
# `security` dependency group — `make strix-scan`, and a deferred check of
# `make verify-integration`. Strix is offensive tooling: it runs only on the
# security lane against the swarm's own repos (conventions/security-scope.md),
# never inside an agent container.
#
# Needs on the host: a Docker-compatible runtime (Strix runs targets in its
# own sandbox container) and a model gateway:
#   LLM_API_BASE  default http://localhost:4000/v1 (the LiteLLM gateway, `make litellm`)
#   STRIX_LLM     default openai/swarm-local
# The report lands in strix_runs/<run>/vulnerabilities.json; validated
# findings become lessons (tag 'strix'), no issues are opened here.
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_PYTHON="${UV_PYTHON:-3.12}"
ENV_DIR="${STRIX_ENV:-${TMPDIR:-/tmp}/strix-env}"
export LLM_API_BASE="${LLM_API_BASE:-http://localhost:4000/v1}"
export STRIX_LLM="${STRIX_LLM:-openai/swarm-local}"
export STRIX_TELEMETRY="${STRIX_TELEMETRY:-false}"

UV_PROJECT_ENVIRONMENT="$ENV_DIR" uv sync -q --frozen --no-install-project --only-group security
"$ENV_DIR/bin/strix" -n -t ./ --scan-mode quick

found=0
for report in strix_runs/*/vulnerabilities.json; do
  [ -s "$report" ] || continue
  found=1
  uv run --no-sync python scripts/strix_findings.py --report "$report" --no-issues
done
[ "$found" = 1 ] || { echo "strix-scan: Strix wrote no vulnerabilities.json" >&2; exit 1; }
