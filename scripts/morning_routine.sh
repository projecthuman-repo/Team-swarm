#!/usr/bin/env bash
# Morning Operator Routine (protected item; extended by v4.1 T6.2 + T1.5).
# Run after every overnight cycle — cron it or run by hand with coffee.
set -uo pipefail
cd "$(dirname "$0")/.."

echo "=== 1/4 DLQ triage (failed tasks -> needs-human issues) ==="
python scripts/dlq_triage.py || true

echo
echo "=== 2/4 SLA report (completion, latency, green rate, headroom savings) ==="
python scripts/sla_report.py || true

echo
echo "=== 3/4 Morning verifier audit (re-audit last night's merges) ==="
python scripts/morning_audit.py || true

echo
echo "=== 4/4 headroom learn (dry-run; --pr to propose corrections) ==="
python scripts/morning_headroom_learn.py || true

echo
echo "Routine complete. Remember: fix the rule/guardrail/skill — not the code."
