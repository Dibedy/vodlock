ALTER TABLE pipeline.jobs ADD COLUMN failure_kind text;
ALTER TABLE pipeline.jobs ADD COLUMN recovery_count integer NOT NULL DEFAULT 0 CHECK (recovery_count >= 0);
ALTER TABLE pipeline.jobs ADD COLUMN wait_count integer NOT NULL DEFAULT 0 CHECK (wait_count >= 0);
ALTER TABLE pipeline.jobs ADD COLUMN recovery_at timestamptz;
