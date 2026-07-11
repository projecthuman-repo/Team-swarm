#!/usr/bin/env sh
# LiteLLM entrypoint (v4.1 T4.3): the hosted-API fallback route is added
# ONLY when ROUTE_API_FALLBACK=1 — an explicit opt-in. With it off, the
# gateway knows no external route at all (fails closed to JetStream retry).
set -eu

CFG=/config/config.yaml
RUNTIME_CFG=/tmp/litellm-config.yaml
cp "$CFG" "$RUNTIME_CFG"

if [ "${ROUTE_API_FALLBACK:-0}" = "1" ] && [ -n "${FALLBACK_API_BASE:-}" ]; then
  cat >> "$RUNTIME_CFG" <<'EOF'

# --- appended by entrypoint: explicit opt-in API fallback ---
router_settings:
  fallbacks:
    - swarm-local: [swarm-api-fallback]
    - swarm-local-35b: [swarm-api-fallback]
EOF
  echo "litellm: hosted API fallback ENABLED (ROUTE_API_FALLBACK=1)"
else
  echo "litellm: local-only routing (fails closed; set ROUTE_API_FALLBACK=1 to opt in)"
fi

exec litellm --config "$RUNTIME_CFG" --port 4000 --host 0.0.0.0
