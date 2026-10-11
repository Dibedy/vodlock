CREATE FUNCTION pipeline.check_canonical_index() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    canonical pipeline.sources;
    broadcast pipeline.broadcasts;
    segment pipeline.segments;
BEGIN
    SELECT * INTO canonical FROM pipeline.sources WHERE id=NEW.canonical_source_id;
    SELECT * INTO broadcast FROM pipeline.broadcasts WHERE id=NEW.broadcast_id;
    SELECT * INTO segment FROM pipeline.segments WHERE id=NEW.segment_id;
    IF canonical.role <> 'canonical' OR canonical.provider <> 'youtube'
       OR canonical.broadcast_id <> NEW.broadcast_id OR canonical.external_id <> broadcast.youtube_id
       OR segment.broadcast_id <> NEW.broadcast_id OR segment.expected_match_id <> NEW.expected_match_id
       OR NEW.generation <> broadcast.generation OR segment.generation <> NEW.generation THEN
        RAISE EXCEPTION 'Index violates canonical broadcast ownership or generation';
    END IF;
    IF NEW.state='final' AND (NEW.archive_revision=0 OR NEW.archive_revision<>broadcast.archive_revision) THEN
        RAISE EXCEPTION 'Index targets a stale archive revision';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER canonical_index_guard BEFORE INSERT OR UPDATE ON pipeline.match_indexes
FOR EACH ROW EXECUTE FUNCTION pipeline.check_canonical_index();

CREATE FUNCTION pipeline.check_source_ownership() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.role='canonical' AND NOT EXISTS (
        SELECT 1 FROM pipeline.broadcasts WHERE id=NEW.broadcast_id AND youtube_id=NEW.external_id
    ) THEN
        RAISE EXCEPTION 'Canonical source must be the official broadcast';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER canonical_source_guard BEFORE INSERT OR UPDATE ON pipeline.sources
FOR EACH ROW EXECUTE FUNCTION pipeline.check_source_ownership();
