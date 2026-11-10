#!/usr/bin/env bash
# Lite-path smoke (Lean & Precise L1.5, run by `make verify-integration`):
# boot the lite spine, bootstrap it, add ollama + forgejo + the relay and one
# backend agent, inject one synthetic task through the outbox, and assert the
# task row reaches status 'done'.
#
# Prerequisites on the runtime host (owner, docs/loop/OWNER_STOP.md):
#   LITE_SMOKE_FORGEJO_TOKEN  the Forgejo bot token (required). OpenBao runs
#                             in dev mode (in memory), so the smoke stores it
#                             at secret/data/swarm/forgejo after bootstrap.
#   a repo swarm/sandbox-repo (FORGEJO_OWNER/FORGEJO_REPO defaults of
#   agent/sandbox.py) in the Forgejo, pushable by that bot on feature/*.
# LITE_SMOKE_TIMEOUT_S (default 1800), LITE_SMOKE_MODEL.
#
# Review R5 — proven right after the spine is up, before anything else:
#   (a) the host reaches localhost on 5432, 4222, 6379 and 8200;
#   (b) from inside the postgres container an outbound TCP connection to a
#       public address (LITE_SMOKE_EGRESS_PROBE, default 1.1.1.1:443) fails
#       within 5 s.
# Exit 3 = (a) failed: this runtime does not forward ports of internal-only
# containers — run with HOSTPORTS=1 (scripts/lite_smoke_modes.sh does that
# automatically). Exit 4 = (b) failed: a spine container has egress.
set -uo pipefail
cd "$(dirname "$0")/.."
RUNTIME="$(command -v podman || command -v docker || true)"
TIMEOUT="${LITE_SMOKE_TIMEOUT_S:-1800}"
MODEL="${LITE_SMOKE_MODEL:-maxwell1500/ornith-9b:Q4_K_M}"
PROBE="${LITE_SMOKE_EGRESS_PROBE:-1.1.1.1:443}"
export HOSTPORTS="${HOSTPORTS:-0}" # the make targets below read it
FULL=(-f deploy/compose.yaml)
[ "$HOSTPORTS" = 1 ] && FULL+=(-f deploy/compose.hostports.yaml)
[ -n "$RUNTIME" ] || { echo "lite-smoke: neither podman nor docker found" >&2; exit 1; }
[ -n "${LITE_SMOKE_FORGEJO_TOKEN:-}" ] || {
  echo "lite-smoke: set LITE_SMOKE_FORGEJO_TOKEN to the Forgejo bot token (see header)" >&2
  exit 1
}

make --no-print-directory up-lite || exit 1

# (a) host -> spine. Services need a moment to listen; 60 s for all four.
reach() { timeout 2 bash -c "</dev/tcp/127.0.0.1/$1" 2>/dev/null; }
unreachable=""
for port in 5432 4222 6379 8200; do
  ok=0
  for _ in $(seq 1 30); do
    if reach "$port"; then ok=1; break; fi
    sleep 2
  done
  [ "$ok" = 1 ] || unreachable="$unreachable $port"
done
if [ -n "$unreachable" ]; then
  echo "lite-smoke: the host cannot reach localhost on port(s):$unreachable (HOSTPORTS=$HOSTPORTS)" >&2
  [ "$HOSTPORTS" = 1 ] || echo "lite-smoke: this runtime needs HOSTPORTS=1" >&2
  exit 3
fi
echo "lite-smoke: host reaches 5432 4222 6379 8200 on localhost (HOSTPORTS=$HOSTPORTS)"

# (b) spine -> outside must fail. First prove the probe itself works in the
# container (bash, timeout, /dev/tcp) against postgres' own port, so a missing
# tool cannot pass for isolation.
pg="$("$RUNTIME" ps -qf name=postgres | head -1)"
"$RUNTIME" exec "$pg" timeout 5 bash -c '</dev/tcp/127.0.0.1/5432' || {
  echo "lite-smoke: the TCP probe does not work inside the postgres container" >&2
  exit 1
}
if "$RUNTIME" exec "$pg" timeout 5 bash -c "</dev/tcp/${PROBE%:*}/${PROBE#*:}" 2>/dev/null; then
  echo "lite-smoke: the postgres container reached $PROBE — the spine has egress (HOSTPORTS=$HOSTPORTS)" >&2
  exit 4
fi
echo "lite-smoke: no outbound connection from the postgres container to $PROBE (HOSTPORTS=$HOSTPORTS)"

make --no-print-directory bootstrap || exit 1
export LITE_SMOKE_FORGEJO_TOKEN # jq reads it from the environment, never from argv
jq -n '{data: {token: env.LITE_SMOKE_FORGEJO_TOKEN}}' \
  | curl -sf -X POST "${OPENBAO_ADDR:-http://localhost:8200}/v1/secret/data/swarm/forgejo" \
      -H "X-Vault-Token: ${OPENBAO_TOKEN:-dev-root}" -d @- >/dev/null || exit 1
make --no-print-directory ollama || exit 1
"$RUNTIME" exec "$("$RUNTIME" ps -qf name=ollama | head -1)" ollama pull "$MODEL" || exit 1
# R4: the agents embed through Ollama (EMBEDDING_BACKEND=api); without the
# model they only degrade to text-only search, but the smoke should run as shipped.
"$RUNTIME" exec "$("$RUNTIME" ps -qf name=ollama | head -1)" \
  ollama pull "${LITE_SMOKE_EMBED_MODEL:-all-minilm}" || exit 1
make --no-print-directory forgejo || exit 1
"$RUNTIME" compose "${FULL[@]}" --profile swarm up -d --build relay agent-backend || exit 1

out="$(uv run --no-sync python scripts/inject_task.py --role backend --title "lite smoke")" || exit 1
echo "$out"
task_id="$(printf '%s\n' "$out" | sed -n 's/^injected backend task \([0-9a-f-]*\):.*/\1/p' | head -1)"
[ -n "$task_id" ] || { echo "lite-smoke: could not read the injected task id" >&2; exit 1; }

pg="$("$RUNTIME" ps -qf name=postgres | head -1)"
deadline=$(( $(date +%s) + TIMEOUT ))
status=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  status="$("$RUNTIME" exec "$pg" psql -U postgres -d swarm -tAc \
    "SELECT status FROM tasks WHERE task_id='$task_id'")"
  case "$status" in
    done) echo "lite-smoke: task $task_id reached done"; exit 0 ;;
    failed) echo "lite-smoke: task $task_id FAILED (see make dlq)" >&2; exit 1 ;;
  esac
  sleep 10
done
echo "lite-smoke: task $task_id still '$status' after ${TIMEOUT}s" >&2
exit 1
