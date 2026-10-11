CREATE SCHEMA IF NOT EXISTS pipeline;

CREATE TABLE pipeline.broadcasts (
    id uuid PRIMARY KEY,
    event text NOT NULL,
    day date NOT NULL,
    region text NOT NULL,
    channel_id text NOT NULL,
    youtube_id text NOT NULL UNIQUE CHECK (youtube_id ~ '^[A-Za-z0-9_-]{11}$'),
    state text NOT NULL DEFAULT 'discovered' CHECK (state IN ('discovered','scheduled','live','ended_waiting_archive','archive_ready','unavailable')),
    actual_start timestamptz,
    actual_end timestamptz,
    seekable boolean NOT NULL DEFAULT false,
    generation integer NOT NULL DEFAULT 1 CHECK (generation > 0),
    archive_revision integer NOT NULL DEFAULT 0,
    reconciled_revision integer NOT NULL DEFAULT 0,
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX broadcasts_day_channel ON pipeline.broadcasts(day, channel_id);

CREATE TABLE pipeline.expected_matches (
    id uuid PRIMARY KEY,
    broadcast_id uuid REFERENCES pipeline.broadcasts(id),
    event text NOT NULL,
    stage text NOT NULL,
    day date NOT NULL,
    team_a text NOT NULL,
    team_b text NOT NULL,
    match_order integer NOT NULL CHECK (match_order > 0),
    best_of integer NOT NULL CHECK (best_of IN (1,3,5,7)),
    region text NOT NULL,
    channel_id text NOT NULL,
    provider text NOT NULL,
    external_id text NOT NULL,
    completion text NOT NULL DEFAULT 'expected' CHECK (completion IN ('expected','running','completed','cancelled')),
    manual_override jsonb NOT NULL DEFAULT '{}',
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(provider, external_id),
    UNIQUE(day, channel_id, match_order)
);
CREATE INDEX expected_matches_broadcast ON pipeline.expected_matches(broadcast_id, match_order);

CREATE TABLE pipeline.sources (
    id uuid PRIMARY KEY,
    broadcast_id uuid NOT NULL REFERENCES pipeline.broadcasts(id),
    provider text NOT NULL CHECK (provider IN ('youtube','twitch')),
    external_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('canonical','full_match','official_twitch','watch_party')),
    state text NOT NULL DEFAULT 'discovered',
    revision integer NOT NULL DEFAULT 1,
    actual_start timestamptz,
    checkpoint_time double precision NOT NULL DEFAULT -1,
    detector_state jsonb NOT NULL DEFAULT '{}',
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(broadcast_id, provider, external_id),
    CHECK (role <> 'canonical' OR provider = 'youtube')
);
CREATE UNIQUE INDEX one_canonical_source ON pipeline.sources(broadcast_id) WHERE role = 'canonical';

CREATE TABLE pipeline.jobs (
    id uuid PRIMARY KEY,
    broadcast_id uuid REFERENCES pipeline.broadcasts(id),
    source_id uuid REFERENCES pipeline.sources(id),
    expected_match_id uuid REFERENCES pipeline.expected_matches(id),
    kind text NOT NULL,
    dedupe_key text NOT NULL UNIQUE,
    generation integer NOT NULL DEFAULT 1,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','waiting_source','retryable','succeeded','needs_review','unsupported')),
    priority integer NOT NULL DEFAULT 0,
    payload jsonb NOT NULL DEFAULT '{}',
    attempt_count integer NOT NULL DEFAULT 0,
    failure_count integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 12 CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_claim ON pipeline.jobs(priority DESC, available_at) WHERE state IN ('queued','retryable','waiting_source','running');

CREATE TABLE pipeline.attempts (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES pipeline.jobs(id),
    lease_token uuid NOT NULL UNIQUE,
    number integer NOT NULL,
    state text NOT NULL DEFAULT 'running',
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    error text,
    UNIQUE(job_id, number)
);

CREATE TABLE pipeline.round_candidates (
    id uuid PRIMARY KEY,
    broadcast_id uuid NOT NULL REFERENCES pipeline.broadcasts(id),
    source_id uuid NOT NULL REFERENCES pipeline.sources(id),
    source_revision integer NOT NULL,
    detector_version text NOT NULL,
    timeline text NOT NULL DEFAULT 'live' CHECK (timeline IN ('live','archive')),
    media_time double precision NOT NULL CHECK (media_time >= 0),
    wall_time timestamptz,
    round_number integer,
    timer integer,
    scores jsonb,
    replay boolean NOT NULL DEFAULT false,
    confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence jsonb NOT NULL DEFAULT '{}',
    diagnostic_ref text,
    accepted boolean NOT NULL DEFAULT false,
    findings jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_id, source_revision, timeline, media_time, detector_version)
);
CREATE INDEX candidates_broadcast_time ON pipeline.round_candidates(broadcast_id, media_time) WHERE accepted;

CREATE TABLE pipeline.fingerprints (
    source_id uuid NOT NULL REFERENCES pipeline.sources(id),
    source_revision integer NOT NULL,
    timeline text NOT NULL CHECK (timeline IN ('live','archive','secondary')),
    media_time double precision NOT NULL,
    hashes jsonb NOT NULL,
    wall_time timestamptz,
    PRIMARY KEY(source_id, source_revision, timeline, media_time)
);

CREATE TABLE pipeline.segments (
    id uuid PRIMARY KEY,
    broadcast_id uuid NOT NULL REFERENCES pipeline.broadcasts(id),
    expected_match_id uuid REFERENCES pipeline.expected_matches(id),
    generation integer NOT NULL,
    start_time double precision NOT NULL,
    end_time double precision NOT NULL,
    state text NOT NULL CHECK (state IN ('indexing','validating','needs_review','provisional','final')),
    rounds jsonb NOT NULL,
    findings jsonb NOT NULL DEFAULT '[]',
    evidence jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(broadcast_id, generation, start_time)
);

CREATE TABLE pipeline.alignments (
    id uuid PRIMARY KEY,
    source_id uuid NOT NULL REFERENCES pipeline.sources(id),
    canonical_source_id uuid NOT NULL REFERENCES pipeline.sources(id),
    source_revision integer NOT NULL,
    canonical_revision integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('reconciliation','recovery','secondary')),
    mapping jsonb NOT NULL,
    diagnostics jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_id, canonical_source_id, source_revision, canonical_revision, kind)
);

CREATE TABLE pipeline.match_indexes (
    id uuid PRIMARY KEY,
    broadcast_id uuid NOT NULL REFERENCES pipeline.broadcasts(id),
    expected_match_id uuid NOT NULL REFERENCES pipeline.expected_matches(id),
    segment_id uuid NOT NULL REFERENCES pipeline.segments(id),
    canonical_source_id uuid NOT NULL REFERENCES pipeline.sources(id),
    generation integer NOT NULL,
    version integer NOT NULL,
    archive_revision integer NOT NULL DEFAULT 0,
    state text NOT NULL CHECK (state IN ('validating','provisional','final','needs_review')),
    shadow boolean NOT NULL DEFAULT true,
    rounds jsonb NOT NULL,
    provenance jsonb NOT NULL,
    findings jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(expected_match_id, generation, version)
);
CREATE INDEX match_indexes_latest ON pipeline.match_indexes(expected_match_id, generation DESC, version DESC);

CREATE TABLE pipeline.chat_archives (
    source_id uuid PRIMARY KEY REFERENCES pipeline.sources(id),
    state text NOT NULL DEFAULT 'live',
    checkpoint_time double precision NOT NULL DEFAULT 0,
    gaps jsonb NOT NULL DEFAULT '[]',
    object_ref text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE pipeline.chat_messages (
    source_id uuid NOT NULL REFERENCES pipeline.sources(id),
    message_id text NOT NULL,
    media_time double precision NOT NULL CHECK (media_time >= 0),
    wall_time timestamptz NOT NULL,
    origin text NOT NULL CHECK (origin IN ('live','vod')),
    message jsonb NOT NULL,
    PRIMARY KEY(source_id, message_id)
);
CREATE INDEX chat_messages_time ON pipeline.chat_messages(source_id, media_time);

CREATE TABLE pipeline.source_events (
    provider text NOT NULL,
    event_id text NOT NULL,
    source_id uuid REFERENCES pipeline.sources(id),
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(provider, event_id)
);
