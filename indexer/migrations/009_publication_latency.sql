ALTER TABLE pipeline.expected_matches ADD COLUMN completion_observed_at timestamptz;
ALTER TABLE pipeline.deployments ADD COLUMN matches jsonb NOT NULL DEFAULT '[]';
