-- v4.1 T4.1 — hardware capacity probe. Each node writes its capabilities
-- on boot; triage/planner route tier-labelled tasks to capable nodes.
CREATE TABLE IF NOT EXISTS node_caps (
  node_id     text PRIMARY KEY,
  free_vram_mb bigint NOT NULL DEFAULT 0,
  total_ram_mb bigint NOT NULL DEFAULT 0,
  cpu_count    int    NOT NULL DEFAULT 0,
  can_serve    text[] NOT NULL DEFAULT '{}',  -- e.g. {9b-q4,35b-q4,api}
  ollama_spill boolean NOT NULL DEFAULT false, -- CPU spill detected (5-30x slower)
  updated_at   timestamptz NOT NULL DEFAULT now()
);
