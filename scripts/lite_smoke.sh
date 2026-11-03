#!/usr/bin/env bash
# Lite-path smoke (Lean & Precise L1.5, run by `make verify-integration`):
# boot the lite spine, bootstrap it, add ollama + forgejo + the relay and one
# backend agent, inject one synthetic task through the outbox, and assert the
# task row reaches status 'done'.
#
# Prerequisites on the runtime host (owner, docs/loop/OWNER_STOP.md): a
# Forgejo bot token stored at OpenBao secret/data/swarm/forgejo and a repo the
# bot can push feature/* branches to. LITE_SMOKE_TIMEOUT_S (default 1800).
set -uo pipefail
cd "$(dirname "$0")/.."
RUNTIME="$(command -v podman || command -v docker || true)"
TIMEOUT="${LITE_SMOKE_TIMEOUT_S:-1800}"
MODEL="${LITE_SMOKE_MODEL:-maxwell1500/ornith-9b:Q4_K_M}"
[ -n "$RUNTIME" ] || { echo "lite-smoke: neither podman nor docker found" >&2; exit 1; }

make --no-print-directory up-lite || exit 1
make --no-print-directory bootstrap || exit 1
make --no-print-directory ollama || exit 1
"$RUNTIME" exec "$("$RUNTIME" ps -qf name=ollama | head -1)" ollama pull "$MODEL" || exit 1
make --no-print-directory forgejo || exit 1
"$RUNTIME" compose -f deploy/compose.yaml --profile swarm up -d --build relay agent-backend || exit 1

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
