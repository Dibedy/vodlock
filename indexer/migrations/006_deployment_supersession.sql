ALTER TABLE pipeline.deployments DROP CONSTRAINT deployments_state_check;
ALTER TABLE pipeline.deployments ADD CONSTRAINT deployments_state_check
    CHECK (state IN ('pushed', 'deployed', 'superseded'));
