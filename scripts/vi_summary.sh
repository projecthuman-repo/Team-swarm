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
failed="$(grep -c '^FAIL' "$LOG")"
if [ "$failed" -gt 0 ]; then
  echo "verify-integration: $failed step(s) FAILED"
  exit 1
fi
echo "verify-integration: all $(wc -l < "$LOG") step(s) passed"
