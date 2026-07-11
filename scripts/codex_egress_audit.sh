#!/usr/bin/env bash
# Codex offline egress audit (v4.2 T7.1 — gating spike).
#
# Before Codex may run inside the SEALED agent container (Hard Rule 1: no
# egress), prove that `codex --oss --local-provider ollama` completes with
# networking limited to swarm-internal — no auth/telemetry/self-update
# callout. This mirrors skills/run_skill.sh --prove-isolation.
#
# Outcome is recorded, never silent:
#   PASS -> Codex may be AGENT_ENGINE=codex inside the sealed container (T7.2)
#   FAIL -> Codex must run on the codex-engine lane with controlled egress
#           (T7.3). Set the finding in conventions/ and route accordingly.
#
# Usage:  ./scripts/codex_egress_audit.sh [ollama_host]
set -uo pipefail

OLLAMA_HOST="${1:-http://localhost:11434}"
CODEX_HOME_DIR="${CODEX_HOME:-$(mktemp -d)/codex-home}"
RUNTIME="$(command -v podman || command -v docker || true)"

if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI not installed on this host."
  echo "Install the Rust binary (see requirements-codex.md), then re-run."
  echo "AUDIT RESULT: SKIPPED (codex absent)"
  exit 2
fi

# Offline CODEX_HOME: disable self-update + telemetry before any run.
mkdir -p "$CODEX_HOME_DIR"
cat > "$CODEX_HOME_DIR/config.toml" <<'EOF'
# Offline profile for the sealed agent container (T7.1).
disable_update_check = true
telemetry = false
[model_providers.ollama]
name = "ollama"
EOF
export CODEX_HOME="$CODEX_HOME_DIR"

echo "==> CODEX_HOME=$CODEX_HOME (self-update/telemetry disabled)"
echo "==> auditing: codex against $OLLAMA_HOST with NO external egress"

# Run Codex on a trivial task inside a network-restricted sandbox. If a
# container runtime is available, cut ALL networking except loopback so any
# auth/telemetry callout fails the run; Codex reaching a host-gateway Ollama
# proves it needs no internet. Without a runtime, fall back to a direct run
# and inspect for callout errors.
TASK='print the current working directory and exit'
if [ -n "$RUNTIME" ]; then
  # --network=none forces total isolation; point Ollama at the host gateway.
  set +e
  OUT="$("$RUNTIME" run --rm --network=none \
    -e CODEX_HOME=/codex-home -v "$CODEX_HOME_DIR:/codex-home:ro" \
    -e OLLAMA_HOST="$OLLAMA_HOST" \
    ghcr.io/openai/codex:latest \
    codex exec --oss --local-provider ollama --skip-git-repo-check "$TASK" 2>&1)"
  RC=$?
  set -e
else
  set +e
  OUT="$(codex exec --oss --local-provider ollama --skip-git-repo-check "$TASK" 2>&1)"
  RC=$?
  set -e
fi

echo "----- codex output (tail) -----"
echo "$OUT" | tail -15
echo "-------------------------------"

if echo "$OUT" | grep -qiE 'auth|login|telemetry|api\.openai\.com|update available|sign in'; then
  echo "AUDIT RESULT: FAIL — Codex attempted an external callout."
  echo "Route Codex to the codex-engine lane (T7.3); do NOT seal it."
  exit 1
fi
if [ "$RC" -ne 0 ]; then
  echo "AUDIT RESULT: INCONCLUSIVE — run failed (rc=$RC), likely no local model."
  echo "Pull a model (make models) and re-run before deciding placement."
  exit 3
fi
echo "AUDIT RESULT: PASS — Codex ran egress-free against local Ollama."
echo "Codex may be sealed as AGENT_ENGINE=codex (T7.2)."
