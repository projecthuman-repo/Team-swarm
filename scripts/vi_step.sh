#!/usr/bin/env bash
# One `make verify-integration` step (Lean & Precise): run the command,
# record PASS/FAIL, never abort the run — every deferred check gets
# evidence, and scripts/vi_summary.sh fails the target at the end if any
# step failed.
#   vi_step.sh --reset                       start a new run (truncate log)
#   vi_step.sh "<task id + name>" <command...>
set -uo pipefail
LOG="${VI_LOG:-${TMPDIR:-/tmp}/verify-integration.log}"

if [ "${1:-}" = "--reset" ]; then
  : > "$LOG"
  exit 0
fi

name="$1"
shift
echo
echo "==> [$name] $*"
if "$@"; then status=PASS; else status=FAIL; fi
printf '%s\t%s\n' "$status" "$name" >> "$LOG"
echo "<== [$name] $status"
exit 0
