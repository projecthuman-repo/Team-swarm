-- Lean & Precise L2.1 — two-stage quality gate timings (first pass).
-- stage 1 = fast lint of the changed Python files; stage 2 = QUALITY_CHECKS
-- + verify_cmd. NULL = the stage did not run (no .py changed / stage 1 red).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS gate_stage1_ms int;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS gate_stage2_ms int;
