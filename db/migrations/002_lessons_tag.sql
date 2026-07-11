-- v4.1 T0.2 (ledger: RESTORATION) — lessons.tag was a protected v4.0 item
-- found missing from both uploaded repos (DR-2). Tags drive the
-- lessons -> knowledge retrieval flywheel: 'headroom-learn',
-- 'morning-audit', 'codex-index', role names, etc.
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS tag text;
CREATE INDEX IF NOT EXISTS lessons_tag_idx ON lessons(tag);
