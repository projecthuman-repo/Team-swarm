-- Review R11 — the three v4.2 capabilities wired into the task flow
-- (T7.7 escalation, T7.8 rubric). Idempotent; mirrored in db/schema.sql.

-- Issue labels as ingested (e.g. swarm-hard). The worker reads them only
-- when one of the opt-in flags is set.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS labels text[] NOT NULL DEFAULT '{}';
-- T7.8: rubric MISS counter. 1 = one revision was forced; 2 = escalate or
-- needs-human. It lives on the row so a redelivery cannot buy a third round.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS rubric_misses int NOT NULL DEFAULT 0;
-- T7.7: set when the task is handed to the escalation lane; never cleared,
-- so a task's PR is recognisable as escalated and a second hand-off is refused.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS escalated_at timestamptz;

-- One row per escalated task (PRIMARY KEY = at most one escalation per
-- task). Written in the same transaction as the outbox row on
-- swarm.tasks.escalation that announces the hand-off.
CREATE TABLE IF NOT EXISTS escalations (
  task_id      uuid PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  reason       text NOT NULL,              -- why the normal path gave up
  failing_diff text NOT NULL,              -- the last attempt, `git diff` vs the base
  gate_output  text NOT NULL,              -- the gate (or rubric) output that failed it
  outcome      text,                       -- pr_opened | the DLQ reason; NULL = waiting
  created_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz
);
