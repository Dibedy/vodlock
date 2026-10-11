ALTER TABLE pipeline.segments ADD COLUMN revision integer NOT NULL DEFAULT 1;
ALTER TABLE pipeline.match_indexes ADD COLUMN segment_revision integer NOT NULL DEFAULT 1;

CREATE FUNCTION pipeline.check_segment_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pipeline.segments WHERE id=NEW.segment_id AND revision=NEW.segment_revision
                   AND expected_match_id=NEW.expected_match_id) THEN
        RAISE EXCEPTION 'Index validation used a stale segment version';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER segment_version_guard BEFORE INSERT OR UPDATE ON pipeline.match_indexes
FOR EACH ROW EXECUTE FUNCTION pipeline.check_segment_version();
