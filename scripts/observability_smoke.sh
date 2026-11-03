#!/usr/bin/env bash
# L3.2 acceptance on the runtime host (`make verify-integration`), after the
# lite smoke has the spine, relay and agent-backend running:
#   1. boot the observability profile (Jaeger, Prometheus, Perses, …)
#   2. every SLA gauge is scraped from the agents' /metrics (Prometheus API)
#   3. a freshly injected task's span appears in Jaeger's API
#   4. Perses serves the provisioned swarm-sla dashboard
# OBS_SMOKE_TIMEOUT_S (default 300).
set -uo pipefail
cd "$(dirname "$0")/.."
RUNTIME="$(command -v podman || command -v docker || true)"
TIMEOUT="${OBS_SMOKE_TIMEOUT_S:-300}"
PROM="${PROMETHEUS_URL:-http://localhost:9090}"
JAEGER="${JAEGER_URL:-http://localhost:16686}"
PERSES="${PERSES_URL:-http://localhost:8090}"
[ -n "$RUNTIME" ] || { echo "obs-smoke: neither podman nor docker found" >&2; exit 1; }

"$RUNTIME" compose -f deploy/compose.yaml --profile observability up -d || exit 1
"$RUNTIME" compose -f deploy/compose.yaml --profile swarm up -d relay agent-backend || exit 1

wait_for() { # $1 = description, rest = command that must succeed
  local what="$1" deadline=$(( $(date +%s) + TIMEOUT ))
  shift
  until "$@" >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "$deadline" ] || { echo "obs-smoke: timed out: $what" >&2; return 1; }
    sleep 5
  done
  echo "obs-smoke: ok — $what"
}

has_series() { # $1 = metric name; Prometheus has at least one sample
  curl -sf "$PROM/api/v1/query?query=$1" | jq -e '.data.result | length > 0'
}
fail=0
for gauge in $(uv run --no-sync python -c \
    "from agent.sla_metrics import GAUGES; print(' '.join(GAUGES))"); do
  wait_for "/metrics serves $gauge (scraped)" has_series "$gauge" || fail=1
done

out="$(uv run --no-sync python scripts/inject_task.py --role backend --title "observability smoke")" || exit 1
task_id="$(printf '%s\n' "$out" | sed -n 's/^injected backend task \([0-9a-f-]*\):.*/\1/p' | head -1)"
trace_id="$(printf '%s' "$task_id" | tr -d '-')"
has_trace() { curl -sf "$JAEGER/api/traces/$trace_id" | jq -e '.data | length > 0'; }
wait_for "task $task_id has a span in Jaeger" has_trace || fail=1

has_dashboard() { curl -sf "$PERSES/api/v1/projects/swarm/dashboards/swarm-sla"; }
wait_for "Perses loads the swarm-sla dashboard" has_dashboard || fail=1
exit "$fail"
