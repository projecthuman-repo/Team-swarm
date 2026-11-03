-- Lean & Precise L2.4 — incremental codex index: one row per source file.
-- scripts/seed_knowledge.py --index-repo upserts on (source) for
-- tag='codex-index', skips rows whose content_sha is unchanged, and deletes
-- rows whose source file is gone. Idempotent.
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS source text,
  ADD COLUMN IF NOT EXISTS content_sha text;
CREATE UNIQUE INDEX IF NOT EXISTS lessons_codex_source_idx
  ON lessons(source) WHERE tag = 'codex-index';
