# Decomposition policy (v4.1 T3.2)

**A single agent is the default.** The verified finding: multi-agent
orchestration multiplies token cost and can merely rival a strong single
model. Fan out only when BOTH hold:

1. The task is labelled `swarm-hard` (triage applies the label), AND
2. the work is genuinely decomposable into subtasks with independent,
   objective verification commands.

When decomposition happens (agent/planner.py):

- every subtask is paired with a `verify_cmd` — a shell command that
  proves it done (tests, ruff, a verifier review). No unverifiable
  subtasks.
- subtasks flow through the normal outbox -> JetStream path with
  `parent_task_id` set; the coordinator replans a verify-failed subtask
  once, then fails the parent to the DLQ.
- parallel review fan-out (Architect / Security / QA lenses,
  agent/verifier.py) is also reserved for `swarm-hard`.

Triage labels drive routing; agents never self-select into fan-out.
