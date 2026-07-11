-- v4.1 T2.1 — first-pass green rate (north-star metric DR-4.1) and the
-- grounded repair loop's cycle count.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS first_pass_green boolean;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS repair_cycles int NOT NULL DEFAULT 0;
