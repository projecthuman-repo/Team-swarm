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
