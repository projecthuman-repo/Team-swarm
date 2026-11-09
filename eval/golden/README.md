# eval/golden — the shadow-eval task pack (v4.1 T6.1)

Golden tasks replayed by `scripts/shadow_eval.py` to compare a candidate
config (model, scaffold, flag) against the current default before ANY
default flips. Grow this pack from real Forgejo history: every task the
swarm handled badly is a candidate golden task.

Each file: `{"title": ..., "role": ..., "verify_cmd": ...}` — the
verify_cmd is the task's objective success criterion.

Report artifacts (`last_report.md`, `default.json`, `candidate.json`)
are written next to this directory and gitignored.

## The pack (v4.1.1, Lean & Precise L3.3)

Ten tasks, at least two per role (`python scripts/shadow_eval.py --dry-run`
lists them with roles, labels and verify commands):

| role | tasks |
|---|---|
| backend | 001 healthz · 002 off-by-one · 003 lint cleanup · 010 rate limiter (**swarm-hard**) |
| frontend | 004 status page · 005 no inline styles |
| review | 006 SQL injection · 007 swallowed error + mutable default |
| triage | 008 vague slowness · 009 split an epic (**swarm-hard**) |

Optional `"labels": ["swarm-hard"]` marks decomposable work; those titles
also start with `[swarm-hard]`, the marker the planner and router read.

Rules for a verify command:

- **One command, no shell.** `verify_cmd` is split with `shlex` and exec'd
  in the cloned workspace — no `&&`, pipes or redirects. Use `pytest …`,
  `ruff …`, or `python -c '<assertions>'`.
- **Deterministic and offline.** No clock, network or randomness; the same
  workspace always gets the same verdict.
- **Runs in the agent image** (Python 3.12 + git — no Node), so frontend
  checks parse the produced HTML/CSS with the standard library.
- `tests/test_golden_pack.py` runs every `python -c` check against a
  correct workspace (must pass) and against a wrong and an empty one (must
  fail). Add a fixture pair with every new task.

Limitation: the engine is shown the verify command as its success
criterion (v4.1 T3.1), so the review tasks measure "produces a correctly
formatted, correctly located, correctly graded finding", not blind bug
discovery. The report compares configs on the same pack, so the bias is
the same on both sides.

`shadow_eval.py` reports per-role green rates and deltas beside the
overall ones; `--single LABEL` + `--report a b --require-not-worse` gate a
change on "not worse than baseline".
