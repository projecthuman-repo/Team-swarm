-- Lean & Precise L2.2 — hybrid retrieval: Postgres full-text search beside
-- the pgvector index, fused with Reciprocal Rank Fusion (k = 60). Exact
-- identifiers (function names, error codes) rank by the sparse branch even
-- when the dense embedding blurs them. Idempotent.
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS fts tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED;
CREATE INDEX IF NOT EXISTS lessons_fts_idx ON lessons USING gin(fts);

-- The sparse CTE orders before its LIMIT (otherwise LIMIT keeps an
-- arbitrary k*4 of the matches) and the score is cast to float8 to match
-- the declared result type (DECISIONS D-023).
CREATE OR REPLACE FUNCTION match_lessons_hybrid(
  query_text text, query_embedding vector, want_tag text, k int DEFAULT 5)
RETURNS TABLE (lesson_id uuid, lesson_text text, score float8)
LANGUAGE sql STABLE AS $$
  WITH dense AS (
    SELECT l.lesson_id, row_number() OVER (ORDER BY l.embedding <=> query_embedding) AS r
    FROM lessons l
    WHERE l.embedding IS NOT NULL AND (want_tag IS NULL OR l.tag = want_tag)
    ORDER BY l.embedding <=> query_embedding LIMIT k * 4),
  sparse AS (
    SELECT l.lesson_id,
           row_number() OVER (ORDER BY ts_rank_cd(l.fts, websearch_to_tsquery('simple', query_text)) DESC) AS r
    FROM lessons l
    WHERE l.fts @@ websearch_to_tsquery('simple', query_text)
      AND (want_tag IS NULL OR l.tag = want_tag)
    ORDER BY r LIMIT k * 4)
  SELECT l.lesson_id, l.text,
         (COALESCE(1.0 / (60 + d.r), 0) + COALESCE(1.0 / (60 + s.r), 0))::float8 AS score
  FROM lessons l
  LEFT JOIN dense d USING (lesson_id)
  LEFT JOIN sparse s USING (lesson_id)
  WHERE d.lesson_id IS NOT NULL OR s.lesson_id IS NOT NULL
  ORDER BY score DESC LIMIT k;
$$;
