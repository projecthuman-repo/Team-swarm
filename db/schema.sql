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
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outbox (
  id           bigserial PRIMARY KEY,
  subject      text  NOT NULL,
  msg_id       text  NOT NULL,        -- -> Nats-Msg-Id header (server-side dedupe)
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
  embedding  vector(384),             -- all-MiniLM-L6-v2 dim
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS lessons_embedding_idx
  ON lessons USING hnsw (embedding vector_cosine_ops);
