# eval/golden — the shadow-eval task pack (v4.1 T6.1)

Golden tasks replayed by `scripts/shadow_eval.py` to compare a candidate
config (model, scaffold, flag) against the current default before ANY
default flips. Grow this pack from real Forgejo history: every task the
swarm handled badly is a candidate golden task.

Each file: `{"title": ..., "role": ..., "verify_cmd": ...}` — the
verify_cmd is the task's objective success criterion.

Report artifacts (`last_report.md`, `default.json`, `candidate.json`)
are written next to this directory and gitignored.
