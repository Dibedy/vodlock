ALTER TABLE pipeline.alignments ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK (revision > 0);
CREATE TABLE pipeline.watchparty_publications (
    source_id uuid NOT NULL REFERENCES pipeline.sources(id),
    expected_match_id uuid NOT NULL REFERENCES pipeline.expected_matches(id),
    canonical_index_id uuid NOT NULL REFERENCES pipeline.match_indexes(id),
    state text NOT NULL CHECK (state IN ('ready', 'waiting_alignment', 'needs_review')),
    findings jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, expected_match_id)
);
CREATE INDEX watchparty_publications_state ON pipeline.watchparty_publications(state, updated_at);
