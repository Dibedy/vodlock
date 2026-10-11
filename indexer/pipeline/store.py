import hashlib
import json
import logging
import uuid
import random
from contextlib import contextmanager
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .errors import StaleAttempt
from .timeline import finding_severity


LOG = logging.getLogger(__name__)


def identifier():
    return uuid.uuid4()


def stable_id(*parts):
    return uuid.uuid5(uuid.NAMESPACE_URL, "spoilless:" + ":".join(str(part) for part in parts))


class Store:
    def __init__(self, database_url):
        self.database_url = database_url

    @contextmanager
    def transaction(self):
        timeout = psycopg.conninfo.conninfo_to_dict(self.database_url).get("connect_timeout", "5")
        with psycopg.connect(self.database_url, connect_timeout=timeout, row_factory=dict_row) as connection:
            with connection.transaction():
                yield connection

    def rows(self, query, parameters=()):
        with self.transaction() as connection:
            return connection.execute(query, parameters).fetchall()

    def one(self, query, parameters=()):
        values = self.rows(query, parameters)
        return values[0] if values else None

    def execute(self, query, parameters=()):
        with self.transaction() as connection:
            return connection.execute(query, parameters).rowcount

    def migrate(self):
        with self.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(87314401)")
            connection.execute("CREATE SCHEMA IF NOT EXISTS pipeline")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS pipeline.migrations (name text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for path in sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")):
                source = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(source.encode()).hexdigest()
                previous = connection.execute(
                    "SELECT checksum FROM pipeline.migrations WHERE name=%s", (path.name,)
                ).fetchone()
                if previous:
                    if previous["checksum"] != checksum:
                        raise ValueError(f"Applied migration changed: {path.name}")
                    continue
                connection.execute(source)
                connection.execute(
                    "INSERT INTO pipeline.migrations(name,checksum) VALUES (%s,%s)", (path.name, checksum)
                )

    def enqueue(
        self, kind, key, broadcast=None, source=None, match=None, payload=None, priority=0, delay=0, connection=None
    ):
        query = """INSERT INTO pipeline.jobs(id,kind,dedupe_key,broadcast_id,source_id,expected_match_id,generation,payload,priority,available_at)
                   VALUES (%s,%s,%s,%s,%s,%s,COALESCE((SELECT generation FROM pipeline.broadcasts WHERE id=%s),1),%s,%s,now()+%s*interval '1 second')
                   ON CONFLICT(dedupe_key) DO UPDATE SET state='queued',
                   payload=CASE WHEN jobs.kind='recover' AND EXCLUDED.payload ? 'terminal_map'
                   AND jobs.generation=EXCLUDED.generation THEN jobs.payload||jsonb_build_object('end',
                   GREATEST((jobs.payload->>'end')::double precision,(EXCLUDED.payload->>'end')::double precision)) ELSE EXCLUDED.payload END,
                   priority=EXCLUDED.priority,
                   source_id=EXCLUDED.source_id,expected_match_id=EXCLUDED.expected_match_id,
                   generation=EXCLUDED.generation, failure_count=0,failure_kind=NULL,recovery_count=0,
                   wait_count=0,recovery_at=NULL,last_error=NULL,available_at=EXCLUDED.available_at, updated_at=now()
                   WHERE (jobs.state='succeeded' AND jobs.kind<>'recover' AND (jobs.kind NOT IN ('validate','validate_upload','reconcile') OR jobs.payload IS DISTINCT FROM EXCLUDED.payload))
                   OR jobs.generation<EXCLUDED.generation OR (jobs.kind IN ('validate','validate_upload','reconcile') AND jobs.state<>'running'
                   AND jobs.generation=EXCLUDED.generation AND jobs.payload IS DISTINCT FROM EXCLUDED.payload)
                   OR (jobs.kind='recover' AND EXCLUDED.payload ? 'terminal_map' AND jobs.generation=EXCLUDED.generation
                   AND jobs.state IN ('queued','waiting_source','retryable','succeeded')
                   AND (EXCLUDED.payload->>'start')::double precision>=(jobs.payload->>'start')::double precision
                   AND (EXCLUDED.payload->>'end')::double precision>(jobs.payload->>'end')::double precision) RETURNING id"""
        parameters = (
            identifier(),
            kind,
            key,
            broadcast,
            source,
            match,
            broadcast,
            Jsonb(payload or {}),
            priority,
            delay,
        )
        if connection is not None:
            return connection.execute(query, parameters).fetchone()
        return self.one(query, parameters)

    def claim(self, lease_seconds=180, job_id=None):
        token = identifier()
        with self.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(87314403)")
            connection.execute(
                "UPDATE pipeline.jobs j SET state='unsupported',failure_kind='superseded',last_error='Superseded broadcast generation',lease_token=NULL,lease_until=NULL,updated_at=now() FROM pipeline.broadcasts b WHERE j.broadcast_id=b.id AND j.generation<>b.generation AND j.state IN ('queued','retryable','waiting_source','running','needs_review')"
            )
            connection.execute(
                "UPDATE pipeline.attempts a SET state='unsupported',ended_at=now(),error='Superseded broadcast generation' FROM pipeline.jobs j WHERE a.job_id=j.id AND a.state='running' AND j.state='unsupported'"
            )
            connection.execute(
                "UPDATE pipeline.attempts a SET state='unsupported',ended_at=now(),error='Job superseded or explicitly retried' FROM pipeline.jobs j WHERE a.job_id=j.id AND a.state='running' AND (j.state<>'running' OR a.lease_token IS DISTINCT FROM j.lease_token)"
            )
            expired = connection.execute("""UPDATE pipeline.jobs SET state=CASE WHEN failure_count+1>=max_attempts THEN 'needs_review' ELSE 'retryable' END,
                         failure_count=failure_count+1,failure_kind='worker_crash',lease_token=NULL,lease_until=NULL,last_error='Worker lease expired',updated_at=now()
                         WHERE state='running' AND lease_until<now() RETURNING id,failure_count""").fetchall()
            for job in expired:
                connection.execute("UPDATE pipeline.jobs SET available_at=now()+%s*interval '1 second' WHERE id=%s",
                                   (backoff(job["failure_count"] - 1, jitter=True), job["id"]))
                connection.execute(
                    "UPDATE pipeline.attempts SET state='expired',ended_at=now(),error='Worker lease expired' WHERE job_id=%s AND state='running'",
                    (job["id"],),
                )
            row = connection.execute("""SELECT j.* FROM pipeline.jobs j LEFT JOIN pipeline.broadcasts b ON b.id=j.broadcast_id
                        WHERE j.state IN ('queued','retryable','waiting_source') AND j.available_at<=now()
                        AND (%s::uuid IS NULL OR j.id=%s)
                        AND j.failure_count<j.max_attempts AND (b.id IS NULL OR j.generation=b.generation)
                        AND NOT EXISTS (
                            SELECT 1 FROM pipeline.jobs active WHERE active.state='running'
                            AND ((j.broadcast_id IS NOT NULL AND active.broadcast_id=j.broadcast_id
                                  AND j.kind<>'chat_gaps' AND active.kind<>'chat_gaps'
                                  AND NOT ((j.source_id IS NULL OR active.source_id IS NULL OR j.source_id<>active.source_id) AND (
                                          (j.kind IN ('twitch_vod','twitch_live','twitch_align','validate_upload') AND active.kind IN ('live','recover','probe_archive','fingerprint_archive','reconcile','segment','validate','reconcile_segment','finalize'))
                                          OR (active.kind IN ('twitch_vod','twitch_live','twitch_align','validate_upload') AND j.kind IN ('live','recover','probe_archive','fingerprint_archive','reconcile','segment','validate','reconcile_segment','finalize')))))
                                 OR (j.source_id IS NOT NULL AND active.source_id=j.source_id)))
                        AND (j.kind<>'validate_upload' OR EXISTS (
                            SELECT 1 FROM pipeline.segments s WHERE s.expected_match_id=j.expected_match_id
                            AND s.broadcast_id=j.broadcast_id AND s.generation=j.generation))
                        ORDER BY CASE WHEN j.kind IN ('segment','validate','finalize','reconcile_segment','export')
                        AND j.available_at<=now()-interval '60 seconds' THEN 1 ELSE 0 END DESC,
                        j.priority+LEAST(20,floor(extract(epoch FROM (now()-j.available_at))/30)) DESC,
                        j.available_at,j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1""", (job_id, job_id)).fetchone()
            if not row:
                return None
            row = connection.execute(
                """UPDATE pipeline.jobs SET state='running',lease_token=%s,lease_until=now()+%s*interval '1 second',
                         attempt_count=attempt_count+1,updated_at=now() WHERE id=%s RETURNING *""",
                (token, lease_seconds, row["id"]),
            ).fetchone()
            attempt = identifier()
            connection.execute(
                "INSERT INTO pipeline.attempts(id,job_id,lease_token,number) VALUES (%s,%s,%s,%s)",
                (attempt, row["id"], token, row["attempt_count"]),
            )
            row["attempt_id"] = attempt
            LOG.info(
                "job_claimed job_id=%s attempt_id=%s broadcast_id=%s source_id=%s kind=%s",
                row["id"],
                attempt,
                row["broadcast_id"],
                row["source_id"],
                row["kind"],
            )
            return row

    def guard(self, connection, job):
        if job.get("broadcast_id"):
            broadcast = connection.execute(
                "SELECT generation FROM pipeline.broadcasts WHERE id=%s FOR UPDATE", (job["broadcast_id"],)
            ).fetchone()
            if not broadcast or broadcast["generation"] != job["generation"]:
                raise StaleAttempt("Broadcast generation changed")
        current = connection.execute(
            "SELECT id FROM pipeline.jobs WHERE id=%s AND state='running' AND lease_token=%s AND lease_until>now() AND generation=%s FOR UPDATE",
            (job["id"], job["lease_token"], job["generation"]),
        ).fetchone()
        if not current:
            raise StaleAttempt("Job lease was lost")

    def heartbeat(self, job, lease_seconds):
        changed = self.execute(
            "UPDATE pipeline.jobs SET lease_until=now()+%s*interval '1 second',updated_at=now() WHERE id=%s AND state='running' AND lease_token=%s AND lease_until>now()",
            (lease_seconds, job["id"], job["lease_token"]),
        )
        if not changed:
            raise StaleAttempt("Job lease was lost")

    def finish(self, job, state="succeeded", error=None, delay=0, dependency=False, failure_kind=None):
        with self.transaction() as connection:
            self.guard(connection, job)
            wait_started = job["payload"].get("dependency_wait_started")
            if dependency:
                wait_started = wait_started or datetime.now(timezone.utc).isoformat()
                if (datetime.now(timezone.utc) - datetime.fromisoformat(wait_started)).total_seconds() >= 14 * 86400:
                    state = "needs_review"
                    failure_kind = "dependency_exhausted"
                    error = "Dependency still unavailable after the 14-day recovery window: " + str(error)
            failure = state in {"waiting_source", "retryable"} and not dependency
            count = job["failure_count"] + int(failure)
            recovery = job.get("recovery_count", 0)
            if state in {"succeeded", "queued"} and failure_kind != "interrupted":
                recovery = 0
            cooling = False
            if failure and count >= job["max_attempts"]:
                if failure_kind in {"network", "timeout", "rate_limit", "source_unavailable", "resource_limit"} and recovery < 2:
                    state = "waiting_source"
                    recovery += 1
                    count = 0
                    delay = max(delay, 21600 * recovery)
                    cooling = True
                else:
                    state = "needs_review"
            connection.execute(
                """UPDATE pipeline.jobs SET state=%s,last_error=%s,failure_count=%s,
                                 available_at=now()+%s*interval '1 second',lease_token=NULL,lease_until=NULL,
                                 failure_kind=%s,recovery_count=%s,wait_count=%s,
                                 recovery_at=CASE WHEN %s THEN now()+%s*interval '1 second' ELSE NULL END,
                                 payload=(payload-'dependency_wait_started')||%s,updated_at=now() WHERE id=%s""",
                (state, error, count if failure else 0, delay, failure_kind, recovery,
                 job.get("wait_count", 0) + 1 if dependency else 0, cooling, delay,
                 Jsonb({"dependency_wait_started": wait_started} if dependency else {}), job["id"]),
            )
            attempt = connection.execute(
                "UPDATE pipeline.attempts SET state=%s,error=%s,ended_at=now() WHERE lease_token=%s RETURNING EXTRACT(EPOCH FROM (ended_at-started_at)) AS seconds",
                ("yielded" if state == "queued" else state, error, job["lease_token"]),
            ).fetchone()
            log = LOG.debug if dependency and job.get("last_error") == error else LOG.info
            log(
                "job_transition job_id=%s attempt_id=%s state=%s reason=%s elapsed_seconds=%s", job["id"], job["attempt_id"], state, error, attempt["seconds"]
            )

    def performance(self, broadcast=None):
        with self.transaction() as connection:
            stages = connection.execute(
                """WITH measured AS (
                   SELECT j.id,j.kind,j.created_at,MIN(a.started_at) AS started,
                   COUNT(a.id) AS attempts,COUNT(a.id) FILTER (WHERE a.state IN ('retryable','expired')) AS retries,
                   COUNT(a.id) FILTER (WHERE a.state='waiting_source') AS source_waits,
                   SUM(EXTRACT(EPOCH FROM (a.ended_at-a.started_at))) AS processing_seconds
                   FROM pipeline.jobs j LEFT JOIN pipeline.attempts a ON a.job_id=j.id
                   WHERE (%s::uuid IS NULL OR j.broadcast_id=%s) AND j.created_at>now()-interval '14 days'
                   GROUP BY j.id)
                   SELECT kind,SUM(attempts) AS attempts,SUM(retries) AS retries,SUM(source_waits) AS source_waits,
                   ROUND(SUM(processing_seconds),3) AS processing_seconds,
                   ROUND(SUM(EXTRACT(EPOCH FROM (started-created_at))),3) AS initial_queue_seconds
                   FROM measured GROUP BY kind ORDER BY processing_seconds DESC NULLS LAST""", (broadcast, broadcast)).fetchall()
            matches = connection.execute(
                """SELECT m.id,m.day,m.match_order,m.completion_observed_at,
                   EXTRACT(EPOCH FROM (now()-m.completion_observed_at)) AS seconds_since_completion_observed,
                   (SELECT MIN(created_at) FROM pipeline.match_indexes i WHERE i.expected_match_id=m.id
                    AND i.state IN ('provisional','final') AND NOT i.shadow) AS first_ready_at,
                   p.first_published_at,p.first_provisional_at,p.first_final_at,
                   EXTRACT(EPOCH FROM (p.first_published_at-m.completion_observed_at)) AS observed_publication_latency_seconds,
                   GREATEST(0,EXTRACT(EPOCH FROM (b.created_at-b.actual_start))) AS broadcast_discovery_delay_seconds
                   FROM pipeline.expected_matches m LEFT JOIN pipeline.broadcasts b ON b.id=m.broadcast_id
                   LEFT JOIN LATERAL (
                     SELECT MIN(d.updated_at) AS first_published_at,
                     MIN(d.updated_at) FILTER (WHERE v->>'pipelineState'='provisional') AS first_provisional_at,
                     MIN(d.updated_at) FILTER (WHERE v->>'pipelineState'='final') AS first_final_at
                     FROM pipeline.deployments d CROSS JOIN LATERAL jsonb_array_elements(d.matches) v
                     WHERE d.state='deployed' AND v->>'expectedMatchId'=m.id::text
                   ) p ON true WHERE (%s::uuid IS NULL OR m.broadcast_id=%s)
                   AND m.day>=current_date-14 ORDER BY m.day DESC,m.match_order""", (broadcast, broadcast)).fetchall()
            blockers = connection.execute(
                   """SELECT kind,state,last_error,failure_kind,COUNT(*) AS jobs,
                   MIN(available_at) AS next_attempt_at,MAX(recovery_count) AS recovery_cycles FROM pipeline.jobs
                   WHERE (%s::uuid IS NULL OR broadcast_id=%s) AND state IN ('waiting_source','retryable','needs_review')
                   AND created_at>now()-interval '14 days' GROUP BY kind,state,last_error,failure_kind""", (broadcast, broadcast)).fetchall()
            rules = connection.execute(
                """SELECT finding->>'code' AS rule,scope,severity,COUNT(*) AS occurrences FROM (
                   SELECT s.findings AS findings,'canonical' AS scope,NULL::text AS severity
                   FROM pipeline.segments s JOIN pipeline.broadcasts b ON b.id=s.broadcast_id
                   WHERE s.generation=b.generation AND b.day>=current_date-14 AND (%s::uuid IS NULL OR b.id=%s)
                   UNION ALL
                   SELECT COALESCE(s.evidence->'full_match_validation'->'source_findings','[]'::jsonb),'upload',
                   COALESCE(s.evidence->'full_match_validation'->>'severity',
                     CASE WHEN NOT COALESCE((s.evidence->'full_match_validation'->>'verified_contradiction')::boolean,false)
                     AND (s.evidence->'full_match_validation'->>'status'='source_needs_review'
                       OR (s.evidence->'full_match_validation'->>'status'='source_incomplete'
                         AND s.evidence->'full_match_validation'->'missing_rounds'='[]'::jsonb))
                     THEN 'warning' ELSE 'blocking' END)
                   FROM pipeline.segments s JOIN pipeline.broadcasts b ON b.id=s.broadcast_id
                   WHERE s.generation=b.generation AND b.day>=current_date-14 AND (%s::uuid IS NULL OR b.id=%s)
                   ) evidence CROSS JOIN LATERAL jsonb_array_elements(findings) finding
                   GROUP BY finding->>'code',scope,severity ORDER BY scope,rule""", (broadcast, broadcast, broadcast, broadcast)).fetchall()
            for rule in rules:
                rule["severity"] = rule["severity"] or finding_severity(rule["rule"])
            for group in (stages, matches, blockers, rules):
                for row in group:
                    for key, value in row.items():
                        if isinstance(value, Decimal):
                            row[key] = float(value)
            return {"target_seconds": 7200, "completion_time_basis": "first_observed_completed; actual match end unavailable",
                    "matches": matches, "stages": stages, "blockers": blockers, "validation_rules": rules}

    def checkpoint(self, job, source, time, state, candidates, fingerprints, revision=None, timeline="live"):
        with self.transaction() as connection:
            self.guard(connection, job)
            current = connection.execute(
                "SELECT * FROM pipeline.sources WHERE id=%s FOR UPDATE", (source["id"],)
            ).fetchone()
            if current["revision"] != source["revision"]:
                raise StaleAttempt("Source revision changed")
            for candidate in candidates:
                connection.execute(
                    """INSERT INTO pipeline.round_candidates(id,broadcast_id,source_id,source_revision,detector_version,timeline,media_time,wall_time,
                                   round_number,timer,scores,replay,confidence,evidence,diagnostic_ref,accepted,findings)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT(source_id,source_revision,timeline,media_time,detector_version) DO UPDATE SET
                                   accepted=EXCLUDED.accepted,round_number=EXCLUDED.round_number,timer=EXCLUDED.timer,scores=EXCLUDED.scores,
                                   confidence=EXCLUDED.confidence,evidence=EXCLUDED.evidence||jsonb_build_object('prior_evidence',round_candidates.evidence),
                                   diagnostic_ref=EXCLUDED.diagnostic_ref,findings=round_candidates.findings||EXCLUDED.findings
                                   WHERE NOT round_candidates.accepted AND (EXCLUDED.accepted
                                   OR (EXCLUDED.round_number IS NOT NULL AND EXCLUDED.timer IS NOT NULL AND EXCLUDED.confidence>=0.85
                                   AND (round_candidates.round_number IS NULL OR round_candidates.timer IS NULL OR round_candidates.confidence<0.85))
                                   OR (EXCLUDED.timeline='archive' AND EXCLUDED.evidence ? 'capture'
                                   AND EXCLUDED.evidence->'provenance'->>'method'='direct_official_archive'
                                   AND EXCLUDED.evidence->'capture' IS DISTINCT FROM round_candidates.evidence->'capture'
                                   AND EXCLUDED.confidence>=round_candidates.confidence
                                   AND EXCLUDED.round_number IS NOT DISTINCT FROM round_candidates.round_number
                                   AND EXCLUDED.timer IS NOT DISTINCT FROM round_candidates.timer
                                   AND EXCLUDED.scores IS NOT DISTINCT FROM round_candidates.scores
                                   AND EXCLUDED.replay IS NOT DISTINCT FROM round_candidates.replay
                                   AND EXCLUDED.evidence->'buy_phase' IS NOT DISTINCT FROM round_candidates.evidence->'buy_phase'))""",
                    (
                        identifier(),
                        source["broadcast_id"],
                        source["id"],
                        source["revision"],
                        candidate["detector_version"],
                        "archive" if timeline == "archive" else "live",
                        candidate["media_time"],
                        candidate.get("wall_time"),
                        candidate.get("round_number"),
                        candidate.get("timer"),
                        Jsonb(candidate.get("scores")),
                        candidate.get("replay", False),
                        candidate["confidence"],
                        Jsonb(candidate.get("evidence", {})),
                        candidate.get("diagnostic_ref"),
                        candidate.get("accepted", False),
                        Jsonb(candidate.get("findings", [])),
                    ),
                )
            for frame in fingerprints:
                connection.execute(
                    """INSERT INTO pipeline.fingerprints(source_id,source_revision,timeline,media_time,hashes,wall_time)
                                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (
                        source["id"],
                        revision or source["revision"],
                        timeline,
                        frame["time"],
                        Jsonb({key: value for key, value in frame.items() if key in {"hash", "gameplayHash"}}),
                        frame.get("wall_time"),
                    ),
                )
            if timeline == "live":
                connection.execute(
                    """UPDATE pipeline.sources SET checkpoint_time=GREATEST(checkpoint_time,%s),detector_state=%s,updated_at=now() WHERE id=%s""",
                    (time, Jsonb(state), source["id"]),
                )

    def fingerprints(self, source, timeline="live", revision=None):
        frames = self.rows(
            "SELECT media_time,hashes FROM pipeline.fingerprints WHERE source_id=%s AND source_revision=%s AND timeline=%s ORDER BY media_time",
            (source["id"], revision or source["revision"], timeline),
        )
        return {
            "duration": max((row["media_time"] for row in frames), default=0) + 10,
            "interval": 10,
            "frames": [{"time": row["media_time"], **row["hashes"]} for row in frames],
        }

    def retry_job(self, job_id, review_only=False):
        with self.transaction() as connection:
            broadcast = None
            if review_only:
                broadcast = connection.execute(
                    "SELECT b.generation FROM pipeline.broadcasts b JOIN pipeline.jobs j ON j.broadcast_id=b.id WHERE j.id=%s FOR UPDATE OF b",
                    (job_id,)).fetchone()
            job = connection.execute("SELECT * FROM pipeline.jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
            if not job or job["state"] == "running":
                raise ValueError("Job is missing or currently leased")
            if review_only:
                if job["state"] != "needs_review" or not broadcast or job["generation"] != broadcast["generation"]:
                    raise ValueError("This task has changed or no longer needs review. Refresh its status.")
            connection.execute(
                "UPDATE pipeline.jobs SET state='queued',failure_count=0,failure_kind=NULL,recovery_count=0,wait_count=0,recovery_at=NULL,payload=payload-'dependency_wait_started',available_at=now(),last_error=NULL,updated_at=now() WHERE id=%s",
                (job_id,))

    def operator_status(self, config):
        settings = (config.settings or {}).get("deployment", {})
        with self.transaction() as connection:
            matches = connection.execute(
                """SELECT m.id,m.broadcast_id,m.day,m.event,m.match_order,m.team_a,m.team_b,
                   s.findings,s.evidence->'full_match_validation'->'source_findings' AS upload_findings,
                   COALESCE(s.state,'pending') AS state,
                   EXISTS (SELECT 1 FROM pipeline.jobs j WHERE j.expected_match_id=m.id
                   AND j.generation=b.generation AND j.state='needs_review') AS needs_review,
                   (SELECT p.state FROM pipeline.watchparty_publications p
                    WHERE p.expected_match_id=m.id ORDER BY p.updated_at DESC LIMIT 1) AS watchparty
                   FROM pipeline.expected_matches m LEFT JOIN pipeline.broadcasts b ON b.id=m.broadcast_id
                   LEFT JOIN LATERAL (SELECT state,findings,evidence FROM pipeline.segments WHERE expected_match_id=m.id
                   AND generation=b.generation ORDER BY updated_at DESC LIMIT 1) s ON true
                   WHERE m.day>=current_date-2 ORDER BY m.day DESC,m.event,m.match_order DESC LIMIT 100""").fetchall()
            tasks = connection.execute(
                """SELECT j.id,j.expected_match_id,j.broadcast_id,j.kind,j.state,j.last_error,j.available_at,
                   j.failure_kind,j.failure_count,j.recovery_count,j.recovery_at,
                   s.role,s.provider,s.external_id,b.youtube_id FROM pipeline.jobs j
                   JOIN pipeline.broadcasts b ON b.id=j.broadcast_id
                   LEFT JOIN pipeline.sources s ON s.id=j.source_id
                   WHERE j.broadcast_id=ANY(%s) AND j.generation=b.generation
                   AND j.state IN ('running','queued','waiting_source','retryable','needs_review')
                   ORDER BY j.updated_at DESC""", ([row["broadcast_id"] for row in matches],)).fetchall()
            finding_labels = {
                "missing_rounds": "Some rounds are missing from the recording.",
                "late_start": "The recording starts after the match has begun.",
                "incomplete_map": "A map recording is incomplete.",
                "incomplete_series": "The recording does not confirm a complete match.",
                "invalid_map_sequence": "The map sequence needs checking.",
                "unassigned_match": "The recording has not been matched to the schedule.",
                "schedule_order_disagreement": "The recording and schedule order disagree.",
                "duplicate_round": "Duplicate round entries need checking.",
                "untrusted_round": "Some detected rounds lack reliable evidence.",
                "impossible_score": "Text recognition produced inconsistent round data.",
                "impossible_score_change": "Text recognition produced inconsistent round progression.",
                "backwards_numbering": "Detected round numbering moves backwards.",
                "suspicious_spacing": "The spacing between detected rounds needs checking.",
            }
            reason_labels = {
                "FFmpeg decoder stalled": "Video decoding returned no frame for 45 seconds.",
                "Twitch scoreboard chunk exceeded": "A video chunk exceeded the bounded disk limit.",
                "Dependency still unavailable after the 14-day recovery window": "A required dependency was still unavailable after fourteen days of automatic recovery.",
                "Alignment evidence changed while processing": "The video revisions changed during synchronization; the current revisions need checking.",
                "Canonical round evidence changed during watch-party verification": "Official round timestamps changed during watch-party verification.",
                "Canonical segment changed during upload comparison": "The official round sequence changed during the separate-upload comparison.",
                "Full-match OCR itself requires review": "Text recognition in the separate full-match upload needs checking.",
                "Full-match sequence disagrees with the broadcast segment": "The separate full-match upload and broadcast recording disagree.",
                "Full-match validation is waiting for the detected broadcast segment": "Waiting for the match to be detected in the official broadcast.",
                "Full-match validation waits for targeted canonical recovery": "Waiting for missing broadcast data to be recovered.",
                "Full-match validation waits for canonical archive coverage": "Waiting for official archive processing to reach this match.",
                "Full-match upload is not processable": "The separate full-match upload is not available for processing.",
                "Segment is incomplete or contains validation findings": "The recording is incomplete or its validation checks have not passed.",
                "Incomplete canonical evidence; waiting for active archive coverage/recovery": "Waiting for archive processing or recovery of missing recording data.",
                "Series completion has not been confirmed": "Waiting for confirmation that the match has finished.",
                "Canonical capture gaps overlap this match; waiting for targeted recovery": "A recording gap affects this match; waiting for recovery.",
                "Recovery exhausted verified sources": "Available verified sources could not recover the missing recording data.",
                "Targeted recovery is waiting for the official archive": "Waiting for the official archive to recover missing recording data.",
                "Archive revision has not been reconciled": "Waiting for the live recording to be matched to the archive.",
                "Archive backfill has not reached the end of this match segment": "Archive processing has not reached the end of this match.",
                "Twitch alignment is waiting for additional verified fingerprint coverage": "More visual matches are needed to synchronize the Twitch stream.",
                "Insufficient unique precise visual anchors for local alignment": "Not enough reliable visual matches to synchronize the streams.",
                "Twitch archive processing requires a verified VOD ID": "Waiting for a verified Twitch archive.",
                "TWITCH_DOWNLOADER is required for VOD chat gap recovery": "Chat recovery requires TwitchDownloader to be configured.",
            }
            for match in matches:
                broadcast = match.pop("broadcast_id")
                findings = match.pop("findings") or []
                upload_findings = match.pop("upload_findings") or []
                match["checks"] = list(dict.fromkeys(
                    finding_labels.get(finding.get("code"), "A recording validation check needs review.")
                    for finding in findings))
                match["tasks"] = []
                for task in tasks:
                    if task["expected_match_id"] != match["id"] and not (
                        task["expected_match_id"] is None and task["broadcast_id"] == broadcast
                        and task["kind"] in {"probe_archive", "fingerprint_archive", "reconcile", "twitch_vod", "twitch_align", "chat_gaps"}):
                        continue
                    error = (task["last_error"] or "") if task["state"] in {"needs_review", "retryable", "waiting_source"} else ""
                    reason = next((message for prefix, message in reason_labels.items() if error.startswith(prefix)), None)
                    if error and not reason:
                        reason = {"network": "The source provider could not be reached.", "timeout": "The video or chat operation exceeded its time limit.",
                                  "rate_limit": "The source provider is rate-limiting requests.", "resource_limit": "This task exceeded the available memory, disk or process resources.",
                                  "source_unavailable": "The recording or provider response is currently unavailable.",
                                  "configuration": "Provider credentials or a required local tool need configuration."}.get(task["failure_kind"])
                    details = upload_findings if task["kind"] == "validate_upload" else findings if task["kind"] in {"validate", "finalize"} else []
                    checks = list(dict.fromkeys(finding_labels.get(finding.get("code"), "A recording validation check needs review.") for finding in details))
                    match["tasks"].append({
                        "id": task["id"], "kind": task["kind"], "state": task["state"], "reason": reason,
                        "checks": checks, "available_at": task["available_at"],
                        "failure_kind": task["failure_kind"], "failure_count": task["failure_count"],
                        "recovery_count": task["recovery_count"], "recovery_at": task["recovery_at"],
                        "scope": "match" if task["expected_match_id"] else "broadcast",
                        "role": task["role"], "provider": task["provider"],
                        "sources": [],
                    })
                    for name, provider, external in (
                        ("Open task source", task["provider"], task["external_id"]),
                        ("Open official broadcast", "youtube", task["youtube_id"]),
                    ):
                        external = external or ""
                        url = (
                            "https://www.youtube.com/watch?v=" + external
                            if provider == "youtube" and len(external) == 11 and all(char.isascii() and (char.isalnum() or char in "-_") for char in external)
                            else "https://www.twitch.tv/videos/" + external
                            if provider == "twitch" and external.isascii() and external.isdecimal() else None)
                        sources = match["tasks"][-1]["sources"]
                        if url and not any(source["url"] == url for source in sources):
                            sources.append({"label": name, "url": url})
            captures = connection.execute(
                """SELECT b.day,s.provider,s.role,
                   CASE WHEN s.role='canonical' THEN b.state ELSE s.state END AS state,s.updated_at,
                   (s.checkpoint_time>=0) AS has_checkpoint,
                   (SELECT j.state FROM pipeline.jobs j WHERE j.source_id=s.id
                    AND j.kind IN ('live','twitch_live') AND j.generation=b.generation
                    ORDER BY j.updated_at DESC LIMIT 1) AS job_state
                   FROM pipeline.sources s JOIN pipeline.broadcasts b ON b.id=s.broadcast_id
                   WHERE (s.role='canonical' AND b.state='live')
                   OR (s.role IN ('official_twitch','watch_party') AND s.state='live')
                   ORDER BY s.updated_at DESC LIMIT 20""").fetchall()
            jobs = connection.execute(
                "SELECT state,COUNT(*) AS count FROM pipeline.jobs WHERE state IN ('running','queued','retryable','waiting_source','needs_review') GROUP BY state"
            ).fetchall()
            activity = connection.execute(
                """SELECT j.id,j.kind,j.updated_at,b.day,s.provider,s.role,s.metadata->>'login' AS creator,
                   m.team_a,m.team_b,j.payload->'activity' AS progress,
                   (SELECT a.started_at FROM pipeline.attempts a WHERE a.job_id=j.id AND a.state='running'
                    ORDER BY a.started_at DESC LIMIT 1) AS started_at,
                   (SELECT jsonb_agg(jsonb_build_object('team_a',e.team_a,'team_b',e.team_b) ORDER BY e.match_order)
                    FROM pipeline.expected_matches e WHERE e.broadcast_id=j.broadcast_id) AS matches
                   FROM pipeline.jobs j LEFT JOIN pipeline.broadcasts b ON b.id=j.broadcast_id
                   LEFT JOIN pipeline.sources s ON s.id=j.source_id
                   LEFT JOIN pipeline.expected_matches m ON m.id=j.expected_match_id
                   WHERE j.state='running' AND j.lease_until>now() AND (b.id IS NULL OR j.generation=b.generation)
                   ORDER BY j.priority DESC,j.updated_at DESC LIMIT 20""").fetchall()
            for item in activity:
                progress = item.pop("progress") or {}
                item["progress"] = {key: value for key, value in progress.items()
                                    if key in {"completed", "total", "map", "round"}
                                    and type(value) is int and 0 <= value <= 10000}
                item["phase"] = progress.get("phase") if progress.get("phase") in {"scoreboard", "searching", "visual_alignment", "chat_recovery"} else None
                match = next((match for match in matches if str(match["id"]) == progress.get("match_id")), None)
                if not match and progress.get("match_id"):
                    match = connection.execute("SELECT team_a,team_b FROM pipeline.expected_matches WHERE id::text=%s AND broadcast_id=(SELECT broadcast_id FROM pipeline.jobs WHERE id=%s)",
                                               (str(progress["match_id"]), item["id"])).fetchone()
                if match:
                    item.update(team_a=match["team_a"], team_b=match["team_b"])
            publication = connection.execute(
                "SELECT state,last_error,available_at FROM pipeline.jobs WHERE dedupe_key='deploy:site'"
            ).fetchone()
            if publication:
                error = publication.pop("last_error") or ""
                publication["reason"] = (
                    "daily_budget" if "24-hour worker budget" in error else
                    "incremental_batch" if "batching incremental" in error else
                    "deployment_error" if error else None)
            budget = connection.execute(
                """WITH usage AS (SELECT repository,branch,publication_kind,created_at FROM pipeline.deployments
                    UNION ALL SELECT d.repository,d.branch,d.publication_kind,r.created_at FROM pipeline.deployment_retries r
                    JOIN pipeline.deployments d ON d.commit_sha=r.commit_sha),
                    scoped AS (SELECT * FROM usage WHERE repository=%s AND branch=%s)
                   SELECT COUNT(*) FILTER (WHERE created_at>clock_timestamp()-interval '24 hours') AS used,
                   COUNT(*) FILTER (WHERE publication_kind='incremental' AND created_at>clock_timestamp()-interval '24 hours') AS incremental_used,
                   (SELECT created_at+interval '24 hours' FROM scoped ORDER BY created_at DESC OFFSET %s LIMIT 1) AS next_slot
                   FROM scoped""",
                (settings.get("repository", ""), settings.get("branch", "main"),
                 settings.get("daily_deployment_limit", 20)-1)).fetchone()
            budget.update(limit=settings.get("daily_deployment_limit", 20),
                          incremental_limit=settings.get("incremental_deployment_limit", 4))
            deployments = connection.execute(
                """SELECT commit_sha,state,created_at,updated_at FROM pipeline.deployments
                   WHERE repository=%s AND branch=%s ORDER BY created_at DESC LIMIT 5""",
                (settings.get("repository", ""), settings.get("branch", "main"))).fetchall()
            verification = connection.execute(
                """SELECT state,(last_error IS NOT NULL) AS has_error FROM pipeline.jobs
                   WHERE kind='verify_deployment' ORDER BY updated_at DESC LIMIT 1""").fetchone()
            return {"matches": matches, "captures": captures, "jobs": jobs, "activity": activity, "publication": publication,
                    "budget": budget, "deployments": deployments, "verification": verification,
                    "updated_at": connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]}

    def status(self, broadcast=None):
        where = " WHERE broadcast_id=%s" if broadcast else ""
        parameters = (broadcast,) if broadcast else ()
        return {
            "jobs": self.rows(
                "SELECT id,broadcast_id,source_id,expected_match_id,kind,state,generation,attempt_count,failure_count,available_at,lease_until,last_error,payload->'checkpoint' AS checkpoint_time FROM pipeline.jobs"
                + where
                + " ORDER BY updated_at DESC LIMIT 100",
                parameters,
            ),
            "segments": self.rows(
                "SELECT id,broadcast_id,expected_match_id,state,findings FROM pipeline.segments"
                + where
                + " ORDER BY start_time LIMIT 100",
                parameters,
            ),
            "sources": self.rows(
                """SELECT s.id,s.broadcast_id,s.provider,s.external_id,s.role,s.checkpoint_time,s.updated_at,
                           (SELECT max(f.media_time) FROM pipeline.fingerprints f WHERE f.source_id=s.id
                            AND f.timeline='archive' AND f.source_revision=b.archive_revision) AS archive_fingerprint_time
                           FROM pipeline.sources s JOIN pipeline.broadcasts b ON b.id=s.broadcast_id"""
                + (" WHERE s.broadcast_id=%s" if broadcast else "")
                + " ORDER BY s.updated_at DESC LIMIT 100",
                parameters,
            ),
            "deployments": self.rows("SELECT commit_sha,state,workflow_id,created_at,updated_at FROM pipeline.deployments ORDER BY created_at DESC LIMIT 10"),
            "watchparties": self.rows("""SELECT p.source_id,p.expected_match_id,p.state,p.findings,p.updated_at
                                         FROM pipeline.watchparty_publications p JOIN pipeline.sources s ON s.id=p.source_id"""
                                       + (" WHERE s.broadcast_id=%s" if broadcast else "") + " ORDER BY p.updated_at DESC LIMIT 100", parameters),
        }


def backoff(failures, jitter=False):
    delay = min(3600, 30 * 2 ** min(max(0, failures), 7))
    return min(3600, random.uniform(.8, 1.2) * delay) if jitter else delay


def json_bytes(value):
    return (json.dumps(value, default=str, ensure_ascii=False, indent=2) + "\n").encode()
