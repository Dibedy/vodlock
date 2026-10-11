ALTER TABLE pipeline.deployments ADD COLUMN publication_kind text NOT NULL DEFAULT 'ready'
    CHECK (publication_kind IN ('ready','incremental'));
