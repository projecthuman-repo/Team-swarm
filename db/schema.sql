-- Overnight Agent Swarm v4.0 — canonical Postgres schema.
-- Postgres is the source of truth: tasks, the transactional outbox
-- (the ONLY publish path into JetStream), and writable vector memory.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS tasks (
  task_id       uuid PRIMARY KEY,
  role          text NOT NULL,
  status        text NOT NULL DEFAULT 'pending',  -- pending|claimed|done|failed
  attempt       int  NOT NULL DEFAULT 0,
  budget_tokens bigint NOT NULL,
  claimed_by    text,
  ext_id        text UNIQUE,                      -- source dedupe key (e.g. forgejo#123)
  title         text,
  spec_ref      text,
  branch        text,
  first_pass_green boolean,                       -- v4.1 T2.1 north-star metric
  repair_cycles int NOT NULL DEFAULT 0,           -- grounded repair loop count
  gate_stage1_ms int,                             -- L2.1 fast lint (first pass)
  gate_stage2_ms int,                             -- L2.1 full checks + verify_cmd
  created_at    timestamptz NOT NULL DEFAULT now(), -- SLA tracking (runbook §17)
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outbox (
  id           bigserial PRIMARY KEY,
  subject      text  NOT NULL,
  msg_id       text  NOT NULL UNIQUE, -- -> Nats-Msg-Id header; UNIQUE = v4.1 T5.1
  payload      bytea NOT NULL,        -- serialized Protobuf
  published_at timestamptz
);

-- Relay scans unpublished rows in insert order.
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx
  ON outbox (id) WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS lessons (
  lesson_id  uuid PRIMARY KEY,
  task_id    uuid REFERENCES tasks,
  text       text NOT NULL,
  tag        text,                    -- retrieval flywheel (v4.1 T0.2 restore)
  embedding  vector(384),             -- all-MiniLM-L6-v2 dim
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS lessons_embedding_idx
  ON lessons USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS lessons_tag_idx ON lessons(tag);

-- Lean & Precise L2.2 — hybrid retrieval (mirrors db/migrations/007_lessons_fts.sql).
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

-- v4.1 T4.1 — per-node hardware capabilities for tier-aware scheduling.
CREATE TABLE IF NOT EXISTS node_caps (
  node_id     text PRIMARY KEY,
  free_vram_mb bigint NOT NULL DEFAULT 0,
  total_ram_mb bigint NOT NULL DEFAULT 0,
  cpu_count    int    NOT NULL DEFAULT 0,
  can_serve    text[] NOT NULL DEFAULT '{}',
  ollama_spill boolean NOT NULL DEFAULT false,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- CDC relay option (runbook §7): logical-replication publication on outbox.
-- wal_level=logical is set in deploy/compose.yaml; relay/relay_cdc.ts tails it.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_publication WHERE pubname = 'outbox_pub') THEN
    CREATE PUBLICATION outbox_pub FOR TABLE outbox;
  END IF;
END $$;
