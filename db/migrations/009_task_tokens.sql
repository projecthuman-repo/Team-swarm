-- Lean & Precise L3.2 — engine tokens per task, the source of the
-- swarm_tokens_per_merged_pr gauge (written by agent_worker.handle_task).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tokens_spent bigint;
