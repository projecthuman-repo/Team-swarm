-- Review R4 — every stored vector carries the embedder that wrote it
-- (`<backend>:<model>`, e.g. `api:all-minilm`). Vectors from different
-- models share the column but not a vector space, so the dense branch of
-- the hybrid search ranks only rows written by the caller's embedder; the
-- rest stay reachable through full-text. Rows written before this
-- migration have embedder NULL: they match by text until
-- `scripts/seed_knowledge.py --reembed` rewrites them. Idempotent.
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS embedder text;
CREATE INDEX IF NOT EXISTS lessons_embedder_idx ON lessons(embedder);

-- The four-argument function (007) cannot tell vector spaces apart.
DROP FUNCTION IF EXISTS match_lessons_hybrid(text, vector, text, int);
CREATE OR REPLACE FUNCTION match_lessons_hybrid(
  query_text text, query_embedding vector, current_embedder text, want_tag text, k int DEFAULT 5)
RETURNS TABLE (lesson_id uuid, lesson_text text, score float8)
LANGUAGE sql STABLE AS $$
  WITH dense AS (
    SELECT l.lesson_id, row_number() OVER (ORDER BY l.embedding <=> query_embedding) AS r
    FROM lessons l
    WHERE l.embedding IS NOT NULL AND l.embedder = current_embedder
      AND (want_tag IS NULL OR l.tag = want_tag)
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
