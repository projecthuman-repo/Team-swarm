-- Optional upgrade: all-MiniLM-L6-v2 (384) -> bge-m3 (1024) embeddings
-- (runbook §18: adopt when retrieval quality > speed).
--
-- Apply, then set EMBEDDING_MODEL=BAAI/bge-m3 and EMBEDDING_DIM=1024 for
-- all agents, install requirements-embeddings.txt, and re-embed existing
-- lessons (scripts/seed_knowledge.py --reembed).

DROP INDEX IF EXISTS lessons_embedding_idx;
ALTER TABLE lessons ALTER COLUMN embedding TYPE vector(1024) USING NULL;
CREATE INDEX lessons_embedding_idx
  ON lessons USING hnsw (embedding vector_cosine_ops);
