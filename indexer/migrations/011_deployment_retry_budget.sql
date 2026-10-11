CREATE TABLE pipeline.deployment_retries (
    commit_sha text NOT NULL REFERENCES pipeline.deployments(commit_sha),
    run_id bigint NOT NULL,
    run_attempt integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (commit_sha, run_id, run_attempt)
);
