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
# Two ways to run, in this order (review R7):
#   1. In the Codex lane image (CODEX_AUDIT_IMAGE, default swarm-codex:dev,
#      built from deploy/Containerfile.codex) attached ONLY to the swarm's
#      internal network (CODEX_AUDIT_NETWORK, default deploy_swarm-internal —
#      `internal: true`, so there is no route out, exactly the sealed agents'
#      condition) with Ollama reachable by its service name. Needs the spine's
#      Ollama up (`make up` or `make ollama`) and a model pulled.
#   2. With a `codex` binary on this host against a local Ollama. This shows
#      the callouts in the output, but the host itself is not sealed.
#
# Verified against Codex rust-v0.167.0 (the pinned CLI): the config keys are
# `check_for_update_on_startup` and `[analytics] enabled`; the local model
# endpoint is CODEX_OSS_BASE_URL (default http://localhost:11434/v1).
#
# Usage:  ./scripts/codex_egress_audit.sh [ollama_host]
set -uo pipefail

OLLAMA_HOST="${1:-http://localhost:11434}"
CODEX_HOME_DIR="${CODEX_HOME:-$(mktemp -d)/codex-home}"
RUNTIME="$(command -v podman || command -v docker || true)"
IMAGE="${CODEX_AUDIT_IMAGE:-swarm-codex:dev}"
NETWORK="${CODEX_AUDIT_NETWORK:-deploy_swarm-internal}"

in_image=0
if [ -n "$RUNTIME" ] \
  && "$RUNTIME" image inspect "$IMAGE" >/dev/null 2>&1 \
  && "$RUNTIME" network inspect "$NETWORK" >/dev/null 2>&1; then
  in_image=1
elif ! command -v codex >/dev/null 2>&1; then
  echo "Neither the lane image $IMAGE on network $NETWORK nor a codex CLI on this host."
  echo "Build the image (make codex-engine) and start the spine, or install the"
  echo "pinned CLI (see requirements-codex.md), then re-run."
  echo "AUDIT RESULT: SKIPPED (codex absent)"
  exit 2
fi

# Offline CODEX_HOME: disable self-update + analytics before any run.
mkdir -p "$CODEX_HOME_DIR"
chmod 0777 "$CODEX_HOME_DIR" # the lane image runs as uid 10001 and writes its state here
cat > "$CODEX_HOME_DIR/config.toml" <<'EOF'
# Offline profile for the sealed agent container (T7.1).
check_for_update_on_startup = false

[analytics]
enabled = false
EOF
export CODEX_HOME="$CODEX_HOME_DIR"

echo "==> CODEX_HOME=$CODEX_HOME (self-update/analytics disabled)"

# Run Codex on a trivial task. If it tries to reach anything but the local
# model — sign-in, telemetry, update check — the run says so or fails.
TASK='print the current working directory and exit'
if [ "$in_image" = 1 ]; then
  echo "==> auditing: $IMAGE on network $NETWORK (internal: no route out), model at ollama:11434"
  set +e
  OUT="$("$RUNTIME" run --rm --network "$NETWORK" \
    -e CODEX_HOME=/codex-home -v "$CODEX_HOME_DIR:/codex-home" \
    -e CODEX_OSS_BASE_URL=http://ollama:11434/v1 \
    "$IMAGE" \
    codex exec --oss --local-provider ollama --skip-git-repo-check "$TASK" 2>&1)"
  RC=$?
  set -e
else
  echo "==> auditing: the host's codex against $OLLAMA_HOST (the host is NOT sealed)"
  set +e
  OUT="$(CODEX_OSS_BASE_URL="${OLLAMA_HOST%/}/v1" \
    codex exec --oss --local-provider ollama --skip-git-repo-check "$TASK" 2>&1)"
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
if [ "$in_image" = 1 ]; then
  echo "AUDIT RESULT: PASS — Codex ran on the internal network with no route out."
else
  echo "AUDIT RESULT: PASS (host run) — no callout seen; repeat in the lane image before sealing."
fi
echo "Codex may be sealed as AGENT_ENGINE=codex (T7.2)."
