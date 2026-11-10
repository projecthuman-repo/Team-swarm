# Escalation, rubric and the fourth reviewer (v4.2 T7.4, T7.7, T7.8)

Three opt-in steps in the task flow (review R11). All are **off by
default** and **fail closed**: with the flags unset a task runs exactly as
it did before, and when a step cannot do its job the change is not pushed.

| flag | what it turns on | when it cannot run |
|---|---|---|
| `ROUTE_API_FALLBACK=1` | a `swarm-hard` task that exhausts its repair budget is handed to the escalation lane instead of failing | `FAILED` with the reason, as on the normal path |
| `RUBRIC_ENABLED=1` | every green change is graded against the rubric before the push | a grader that cannot be reached is a MISS |
| `CODEX_REVIEW=1` | `swarm-hard` reviews get Codex as a fourth reviewer | recorded; blocks only with `CODEX_REVIEW_REQUIRED=1` |

A task is `swarm-hard` when its issue carries that label (stored in
`tasks.labels` at ingest) or its title contains `[swarm-hard]`.

## Escalation lane (T7.7)

1. **Hand-off.** The worker's last gate is red after `MAX_REPAIR_CYCLES`.
   For a `swarm-hard` task with the flag on, one Postgres transaction moves
   the task to `escalated`, stores the failing diff and the gate output in
   `escalations`, and writes the outbox row that announces it on
   `swarm.tasks.escalation`. The relay publishes it; the worker never does.
2. **The lane is host-run:** `make escalation`
   (`uv sync --group agent --extra escalation`, its own environment). It is
   never part of a sealed agent container and never in an image — it needs
   Anthropic egress, and `claude-agent-sdk`'s wheel bundles a CLI under
   commercial terms, so no image closure may contain it (the license gate's
   tests assert that).
3. **The lane worker** binds the durable consumer `escalation`, claims the
   task by compare-and-set (`escalated` → `claimed`), calls `escalate()`
   with the stored diff and gate output (the Claude key is an OpenBao
   lease), applies the returned patch to a fresh clone of the base branch
   and runs the same two-stage gate, once.
4. **Green:** it pushes `feature/<task_id>` and emits `PR_OPENED` exactly
   as the normal path does, with `escalated=1` in the event and the trace;
   `tasks.escalated_at` marks the row.
   **Anything else** — the patch does not apply, the gate is red or times
   out, the SDK is not installed or fails, a key lease fails — is `FAILED`
   with the reason: status `failed`, a `FAILED` event, the reason also in
   `escalations.outcome`, and `term()` so the message is not redelivered —
   what a poisoned task gets on the normal path. Nothing is pushed.
5. **At most one escalation per task.** `escalations.task_id` is the
   primary key and `escalated_at` is never cleared: a second hand-off is
   refused and the task fails as it would without the flag. `make
   dlq-replay` of a message from the lane's subject re-injects the task on
   its role's subject — a normal attempt.

The lane's gate executes model-written code on the host, outside the
container seal. Its environment is an allowlist — the tool path, the
locale, the git identity — so it holds none of the swarm's secrets or
service addresses, and the bot token is not in the clone it runs in. That
narrows what the code can read; it does not sandbox it. Run the lane on a
host where that is acceptable.

"DLQ" in this file means what the normal path does with a poisoned task:
status `failed`, a `FAILED` event with the reason, `term()`. The
`SWARM_DLQ` stream itself captures only messages that exhaust
`max-deliver`.

## Rubric (T7.8)

The pre-push review step grades the hand-off bundle (spec + diff) with
`grade_rubric`:

- **HIT** — push.
- **first MISS** — exactly one revision: the engine runs again with the
  grader's notes, then the gate, then the grader.
- **second MISS** — no further revision. The task is escalated when
  escalation is enabled for it (`swarm-hard` and `ROUTE_API_FALLBACK=1`),
  otherwise it takes the needs-human path (it fails with the reason, as
  above).

The counter is `tasks.rubric_misses`. It lives on the row, so a redelivered
task cannot buy another revision: there is never a third round.

## Codex, the fourth reviewer (T7.4)

When the review step runs for a `swarm-hard` task, the three lenses —
Architect, Security, QA — review the bundle; with `CODEX_REVIEW=1`,
`codex_review.review` joins them and its finding is in the same aggregate.
Any BLOCK means the change is not pushed (needs-human). A lens ERROR blocks
too. A Codex ERROR — the CLI is missing, its output cannot be parsed — is
recorded in the findings and logged, and blocks only when
`CODEX_REVIEW_REQUIRED=1`.

The review step exists only when a flag asks for it: `RUBRIC_ENABLED=1`
(every task), or `CODEX_REVIEW=1` (swarm-hard tasks). A task that is not
`swarm-hard` gets no lens review — a single agent is the default (T3.2).

## Rules for agents

- Never call `escalate()` or import `claude_agent_sdk` from code that runs
  in an agent container.
- Never publish the hand-off yourself: write the outbox row.
- Never add the `escalation` extra to an image, a compose service or a
  Kubernetes manifest.
