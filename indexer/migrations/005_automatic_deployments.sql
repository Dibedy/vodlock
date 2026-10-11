CREATE TABLE pipeline.deployments (
    commit_sha text PRIMARY KEY CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    repository text NOT NULL,
    branch text NOT NULL,
    state text NOT NULL CHECK (state IN ('pushed', 'deployed')),
    workflow_id bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX deployments_state_created ON pipeline.deployments(state, created_at);
