#!/usr/bin/env bash
# Review R5, run by `make verify-integration`: find out which network mode
# this host needs, and prove reachability and isolation in that mode.
#   1. the lite smoke as shipped (spine on the internal network only);
#   2. only if the host could not reach the spine ports (smoke exit 3): stop
#      the spine and run the smoke again with HOSTPORTS=1 — it must then
#      pass both the reachability and the no-egress check.
# The mode that worked is written to $HOST_MODE_FILE and printed by
# scripts/vi_summary.sh; use it for every later `make up` / `make up-lite`.
set -uo pipefail
cd "$(dirname "$0")/.."
HOST_MODE_FILE="${HOST_MODE_FILE:-${TMPDIR:-/tmp}/verify-integration.host-mode}"
: > "$HOST_MODE_FILE"

HOSTPORTS=0 ./scripts/lite_smoke.sh
rc=$?
if [ "$rc" -eq 0 ]; then
  echo "host network mode: default (ports of the internal-only spine are reachable)" | tee "$HOST_MODE_FILE"
  exit 0
fi
if [ "$rc" -ne 3 ]; then
  exit "$rc"
fi

echo
echo "lite-smoke-modes: the host cannot reach the internal-only spine; retrying with HOSTPORTS=1"
HOSTPORTS=0 make --no-print-directory down-lite || exit 1
HOSTPORTS=1 ./scripts/lite_smoke.sh
rc=$?
if [ "$rc" -eq 0 ]; then
  echo "host network mode: HOSTPORTS=1 (run make up / make up-lite with HOSTPORTS=1 on this host)" \
    | tee "$HOST_MODE_FILE"
fi
exit "$rc"
