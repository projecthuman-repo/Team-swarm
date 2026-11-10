#!/usr/bin/env bash
# Summary of `make verify-integration`: prints every step's result and exits
# non-zero when any step failed or none ran (deferred is not skipped).
set -uo pipefail
LOG="${VI_LOG:-${TMPDIR:-/tmp}/verify-integration.log}"

echo
echo "== verify-integration summary =="
if [ ! -s "$LOG" ]; then
  echo "no steps recorded"
  exit 1
fi
column -t -s $'\t' "$LOG" 2>/dev/null || cat "$LOG"
# R5: which network mode the lite smoke found this host needs
HOST_MODE_FILE="${HOST_MODE_FILE:-${TMPDIR:-/tmp}/verify-integration.host-mode}"
[ -s "$HOST_MODE_FILE" ] && cat "$HOST_MODE_FILE"
failed="$(grep -c '^FAIL' "$LOG")"
if [ "$failed" -gt 0 ]; then
  echo "verify-integration: $failed step(s) FAILED"
  exit 1
fi
skipped="$(grep -c '^SKIP' "$LOG")"
echo "verify-integration: $(grep -c '^PASS' "$LOG") step(s) passed, $skipped skipped (opt-in, precondition off)"
