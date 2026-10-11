import logging
import math
import statistics
import hashlib
import json
from datetime import timedelta
from typing import Any

import cv2
import numpy as np
from psycopg.types.json import Jsonb
from storyboard_align import ALIGNER_VERSION
from detector import DETECTOR_VERSION as OCR_VERSION

from .detection import DETECTOR_VERSION, CandidateDetector, observation_from_dict, redetect_candidates
from .errors import ContinueJob, NeedsReview, StaleAttempt, WaitingSource, WaitingWork
from .media import MediaAnalysis
from .publishing import persist_index
from .store import identifier, stable_id
from .timeline import (
    broadcast_rounds,
    mapped_time,
    piecewise_alignment,
    reconcile_rounds,
    segment_matches,
    validate_rounds,
)
from .youtube import YouTube


LOG = logging.getLogger(__name__)


class Processing:
    def __init__(self, store, config, storage, youtube=None, media=None):
        self.store = store
        self.config = config
        self.storage = storage
        self.youtube = youtube or YouTube(archive_client=(config.settings or {}).get("youtube_archive_client"))
        self.media = media or MediaAnalysis()
        self.stop: Any = None

    def check_stop(self):
        if self.stop is not None and self.stop.is_set():
            raise InterruptedError("Worker stopped after its last durable checkpoint")

    def context(self, job):
        broadcast = self.store.one("SELECT * FROM pipeline.broadcasts WHERE id=%s", (job["broadcast_id"],))
        source = (
            self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (job["source_id"],))
            if job.get("source_id")
            else self.store.one(
                "SELECT * FROM pipeline.sources WHERE broadcast_id=%s AND role='canonical'", (broadcast["id"],)
            )
        )
        matches = self.store.rows(
            "SELECT * FROM pipeline.expected_matches WHERE broadcast_id=%s ORDER BY match_order", (broadcast["id"],)
        )
        aliases = {}
        for match in matches:
            aliases.update(match["metadata"].get("aliases", {}))
            aliases.setdefault(match["team_a"], [match["team_a"]])
            aliases.setdefault(match["team_b"], [match["team_b"]])
        return broadcast, source, matches, aliases

    def save_diagnostic(self, source, candidate, frame):
        if (
            candidate["accepted"]
            or candidate["findings"]
            and candidate["findings"][0] in {"unstable_clock", "score_round_disagreement", "unconfirmed_map_reset", "pending_map_reset"}
        ):
            loaded, image = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if loaded:
                key = f"diagnostics/{source['id']}/{source['revision']}/{candidate['media_time']:.3f}.jpg"
                candidate["diagnostic_ref"] = self.storage.put(key, image.tobytes(), "image/jpeg")

    def live(self, job):
        broadcast, source, matches, aliases = self.context(job)
        if broadcast["state"] != "live":
            return
        start = source["actual_start"] or broadcast["actual_start"]
        if not start:
            raise WaitingSource("Official start time is required to map live media timestamps")
        remote = self.youtube.resolve(source["external_id"], live=True)
        segments = self.media.manifest(remote, start_sequence=source["detector_state"].get("media_sequence", 0))
        detector = CandidateDetector(source["detector_state"])
        checkpoint = source["checkpoint_time"]
        processed = 0
        for segment in segments:
            wall = segment["wall_time"]
            if wall is None:
                raise WaitingSource("HLS has no program-date-time anchor; guessing a timeline offset is forbidden")
            base = (wall - start).total_seconds()
            if base < 0 or base + segment["duration"] <= checkpoint:
                continue
            if base > checkpoint + 30 and checkpoint >= 0:
                LOG.warning(
                    "live_capture_gap broadcast_id=%s source_id=%s range=%s-%s",
                    broadcast["id"],
                    source["id"],
                    checkpoint,
                    base,
                )
                self.store.enqueue(
                    "recover",
                    f"recover:{source['id']}:{checkpoint:.3f}:{base:.3f}",
                    broadcast=broadcast["id"],
                    source=source["id"],
                    payload={"start": max(0, checkpoint - 15), "end": base + 15, "stage": 2},
                    priority=20,
                )
            elif checkpoint < 0 and base > 30:
                self.store.enqueue(
                    "recover",
                    f"late-start:{source['id']}",
                    broadcast=broadcast["id"],
                    source=source["id"],
                    payload={"start": 0, "end": base + 15, "stage": 2},
                    priority=20,
                )
            candidates, fingerprints = [], []
            frames = ((round(base + offset, 3), frame) for offset, frame in self.media.live_frames(remote, segment)
                      if round(base + offset, 3) > checkpoint)
            for sample, extra, frame in self.media.scan_frames(frames, aliases, compact=True, detector=detector):
                self.check_stop()
                time = sample.time
                if time <= checkpoint:
                    continue
                offset = time - base
                candidate = detector.observe(sample, (wall + timedelta(seconds=offset)).isoformat(), extra)
                candidates.extend(detector.confirmed_candidates)
                if sample.round is not None or sample.timer is not None:
                    self.save_diagnostic(source, candidate, frame)
                    candidates.append(candidate)
                if int(time) % self.config.fingerprint_seconds == 0:
                    fingerprints.append(
                        {"time": time, "wall_time": candidate["wall_time"], **self.media.fingerprint(frame)}
                    )
                checkpoint = time
            state = {**detector.state(), "media_sequence": segment.get("sequence", 0)}
            self.store.checkpoint(job, source, checkpoint, state, candidates, fingerprints)
            self.store.heartbeat(job, self.config.lease_seconds)
            processed += 1
            if processed >= 4:
                break
        self.store.enqueue("segment", f"segment:{broadcast['id']}", broadcast=broadcast["id"])
        if segments[0]["wall_time"]:
            earliest = (segments[0]["wall_time"] - start).total_seconds()
            accepted = self.store.one(
                "SELECT min(media_time) AS first FROM pipeline.round_candidates WHERE broadcast_id=%s AND accepted",
                (broadcast["id"],),
            )
            seekable = bool(accepted and accepted["first"] is not None and earliest <= accepted["first"] - 15)
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                connection.execute(
                    "UPDATE pipeline.broadcasts SET seekable=%s,updated_at=now() WHERE id=%s",
                    (seekable, broadcast["id"]),
                )

    def probe_archive(self, job):
        broadcast, source, _, _ = self.context(job)
        remote = self.youtube.resolve(source["external_id"])
        duration = float(remote.get("duration") or 0)
        if duration <= 0:
            raise WaitingSource("YouTube archive duration is not ready")
        for point in [0, max(0, duration - 10)]:
            frames = list(self.media.archive_fingerprints(remote, point, min(point + 2, duration), interval=1))
            if not frames:
                raise WaitingSource("YouTube archive is not seekable at both ends yet")
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            current = connection.execute("SELECT * FROM pipeline.broadcasts WHERE id=%s", (broadcast["id"],)).fetchone()
            revision = max(1, current["archive_revision"])
            connection.execute(
                "UPDATE pipeline.broadcasts SET state='archive_ready',seekable=true,archive_revision=%s,updated_at=now() WHERE id=%s",
                (revision, broadcast["id"]),
            )
            self.store.enqueue(
                "reconcile",
                f"reconcile:{broadcast['id']}:{revision}",
                broadcast=broadcast["id"],
                source=source["id"],
                payload={"aligner_version": ALIGNER_VERSION},
                priority=30,
                connection=connection,
            )
        LOG.info("archive_ready broadcast_id=%s revision=%s", broadcast["id"], revision)

    def redetect_canonical(self, candidates, alignment=None):
        if alignment is None:
            return redetect_candidates(candidates)[0]
        live, _ = redetect_candidates([value for value in candidates if value["timeline"] == "live"])
        anchors = []
        for value in live:
            try:
                anchors.append({**value, "timeline": "archive",
                                "media_time": mapped_time(value["media_time"], alignment),
                                "evidence": {**value["evidence"], "start": mapped_time(value["evidence"]["start"], alignment)}})
            except NeedsReview:
                LOG.warning("round_outside_archive_anchors candidate_id=%s", value["id"])
        archive, _ = redetect_candidates([value for value in candidates if value["timeline"] == "archive"], anchors)
        return anchors + archive

    def segment(self, job):
        broadcast, source, matches, _ = self.context(job)
        candidates = self.store.rows(
            """SELECT * FROM pipeline.round_candidates WHERE broadcast_id=%s AND source_id=%s
                                      AND source_revision=%s ORDER BY media_time""",
            (broadcast["id"], source["id"], source["revision"]),
        )
        contexts = {}
        for candidate in candidates:
            reference = candidate.get("diagnostic_ref")
            if not candidate["accepted"] or not reference or isinstance(candidate["evidence"].get("intro_context"), dict):
                continue
            if reference not in contexts:
                try:
                    frame = cv2.imdecode(np.frombuffer(self.storage.get(reference), dtype=np.uint8), cv2.IMREAD_COLOR)
                except FileNotFoundError:
                    LOG.warning("diagnostic_unavailable broadcast_id=%s candidate_id=%s", broadcast["id"], candidate["id"])
                    continue
                if frame is None:
                    raise NeedsReview("Stored diagnostic image cannot be decoded: " + reference)
                contexts[reference] = self.media.intro_context(frame)
                self.store.heartbeat(job, self.config.lease_seconds)
            candidate["evidence"]["intro_context"] = contexts[reference]
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                connection.execute(
                    "UPDATE pipeline.round_candidates SET evidence=jsonb_set(evidence,'{intro_context}',%s) WHERE id=%s",
                    (Jsonb(contexts[reference]), candidate["id"]),
                )
        rounds = []
        archive_alignment = (
            self.store.one(
                "SELECT mapping FROM pipeline.alignments WHERE source_id=%s AND kind='reconciliation' AND canonical_revision=%s",
                (source["id"], broadcast["archive_revision"]),
            )
            if broadcast["archive_revision"]
            else None
        )
        candidates = self.redetect_canonical(candidates, archive_alignment["mapping"] if archive_alignment else None)
        for candidate in candidates:
            evidence = candidate["evidence"]
            start = evidence["start"]
            if candidate["timeline"] == "live" and archive_alignment:
                try:
                    start = mapped_time(start, archive_alignment["mapping"])
                except NeedsReview:
                    LOG.warning(
                        "round_outside_archive_anchors broadcast_id=%s candidate_id=%s",
                        broadcast["id"],
                        candidate["id"],
                    )
                    continue
            elif candidate["timeline"] == "archive" and not archive_alignment:
                continue
            rounds.append(
                {
                    "map": evidence["broadcast_map"],
                    "round": candidate["round_number"],
                    "start": start,
                    "confidence": candidate["confidence"],
                    "scores": candidate["scores"],
                    "teams": evidence.get("teams"),
                    "map_reset": evidence.get("map_reset", False),
                    "candidate_id": str(candidate["id"]),
                    "provenance": evidence.get("provenance", {"method": "live_official_youtube"}),
                }
            )
        for segment in segment_matches(broadcast_rounds(rounds), matches):
            segment["evidence"]["timeline"] = "archive" if archive_alignment else "live"
            match = segment["match"]
            if archive_alignment and match and match["completion"] == "completed":
                for finding in segment["findings"]:
                    if finding["code"] == "incomplete_map" and not finding.get("range"):
                        terminal = [value for value in segment["rounds"] if value["map"] == finding["map"]]
                        archive_end = self.store.one("SELECT max(media_time)+2 AS end_time FROM pipeline.fingerprints WHERE source_id=%s AND source_revision=%s AND timeline='archive'", (source["id"], broadcast["archive_revision"]))["end_time"]
                        if terminal and archive_end and terminal[-1]["start"] < archive_end:
                            finding["range"] = [max(0, terminal[-1]["start"] - 10), archive_end]
                            finding["terminal_tail"] = True
            state = segment["state"]
            if match and match["completion"] != "completed":
                state = "indexing"
            segment_id = stable_id("segment", broadcast["id"], job["generation"], segment["start"])
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                previous = connection.execute(
                    "SELECT * FROM pipeline.segments WHERE broadcast_id=%s AND generation=%s AND (start_time=%s OR expected_match_id=%s) ORDER BY (start_time=%s) DESC,revision DESC LIMIT 1 FOR UPDATE",
                    (broadcast["id"], job["generation"], segment["start"], match["id"] if match else None, segment["start"]),
                ).fetchone()
                if previous:
                    segment_id = previous["id"]
                if previous and previous["evidence"].get("manual_assignment"):
                    match = next((item for item in matches if item["id"] == previous["expected_match_id"]), None)
                    if match:
                        segment["evidence"]["manual_assignment"] = previous["evidence"]["manual_assignment"]
                        segment["findings"] = [
                            finding
                            for finding in segment["findings"]
                            if finding["code"] not in {"unassigned_match", "schedule_order_disagreement"}
                        ]
                        state = "validating" if match["completion"] == "completed" else "indexing"
                active_coverage = bool(match and self.pending_coverage(job, source, {"expected_match_id": match["id"], "start_time": segment["start"], "end_time": segment["end"], "evidence": segment["evidence"]}, connection))
                coverage_pending = bool(active_coverage and segment["findings"] and all(finding["code"] in {"incomplete_map", "incomplete_series", "missing_rounds", "late_start"} for finding in segment["findings"]))
                if coverage_pending:
                    state = "indexing"
                persisted_segment = connection.execute(
                    """INSERT INTO pipeline.segments(id,broadcast_id,expected_match_id,generation,start_time,end_time,state,rounds,findings,evidence)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET
                                   expected_match_id=EXCLUDED.expected_match_id,start_time=EXCLUDED.start_time,end_time=EXCLUDED.end_time,rounds=EXCLUDED.rounds,
                                   findings=EXCLUDED.findings,evidence=EXCLUDED.evidence||CASE WHEN segments.evidence ? 'full_match_validation' THEN jsonb_build_object('full_match_validation',segments.evidence->'full_match_validation') ELSE '{}'::jsonb END,
                                   revision=segments.revision+CASE WHEN segments.rounds IS DISTINCT FROM EXCLUDED.rounds OR segments.findings IS DISTINCT FROM EXCLUDED.findings OR segments.expected_match_id IS DISTINCT FROM EXCLUDED.expected_match_id OR segments.evidence->'closed' IS DISTINCT FROM EXCLUDED.evidence->'closed' THEN 1 ELSE 0 END,
                                   state=CASE WHEN segments.state IN ('final','provisional') THEN segments.state ELSE EXCLUDED.state END,updated_at=now() RETURNING id,revision""",
                    (
                        segment_id,
                        broadcast["id"],
                        match["id"] if match else None,
                        job["generation"],
                        segment["start"],
                        segment["end"],
                        state,
                        Jsonb(segment["rounds"]),
                        Jsonb(segment["findings"]),
                        Jsonb(segment["evidence"]),
                    ),
                ).fetchone()
                segment_id = persisted_segment["id"]
                if match and match["completion"] == "completed":
                    self.store.enqueue(
                        "validate",
                        f"validate:{segment_id}:g{job['generation']}",
                        broadcast=broadcast["id"],
                        source=source["id"],
                        match=match["id"],
                        payload={"segment_id": str(segment_id), "segment_revision": persisted_segment["revision"], "coverage_pending": coverage_pending, "shadow": self.config.shadow_for(broadcast)},
                        priority=40,
                        connection=connection,
                    )
                if match:
                    for upload_source in connection.execute("SELECT * FROM pipeline.sources WHERE broadcast_id=%s AND role='full_match' AND metadata->>'expected_match_id'=%s", (broadcast["id"], str(match["id"]))).fetchall():
                        self.store.enqueue("validate_upload", f"validate-upload:{upload_source['id']}", broadcast=broadcast["id"], source=upload_source["id"], match=match["id"], payload={"segment_revision": persisted_segment["revision"], "coverage_pending": active_coverage, "ocr_version": OCR_VERSION}, priority=-20, connection=connection)
                active_recovery = []
                for finding in segment["findings"]:
                    if finding["code"] in {"missing_rounds", "late_start", "invalid_map_sequence", "incomplete_map"} and finding.get(
                        "range"
                    ):
                        start, end = finding["range"]
                        key = f"recover:{source['id']}:{start:.3f}:{end:.3f}:{OCR_VERSION}"
                        payload = {"start": start, "end": end, "stage": 2, "timeline": segment["evidence"]["timeline"]}
                        if finding.get("terminal_tail"):
                            key = f"recover:{source['id']}:tail:{segment_id}:m{finding['map']}:s{source['revision']}:r{broadcast['archive_revision']}:{OCR_VERSION}"
                            prior = connection.execute("SELECT payload FROM pipeline.jobs WHERE dedupe_key=%s AND generation=%s", (key, job["generation"])).fetchone()
                            if prior and start < prior["payload"]["start"]:
                                key = f"recover:{source['id']}:{start:.3f}:{end:.3f}:{OCR_VERSION}"
                            else:
                                payload["terminal_map"] = finding["map"]
                        active_recovery.append(key)
                        self.store.enqueue(
                            "recover",
                            key,
                            broadcast=broadcast["id"],
                            source=source["id"],
                            match=match["id"] if match else None,
                            payload=payload,
                            priority=40 if match and match["completion"] == "completed" else 20,
                            connection=connection,
                        )
                if match:
                    connection.execute("UPDATE pipeline.jobs SET state='unsupported',last_error='Recovery range superseded by current segment evidence',updated_at=now() WHERE broadcast_id=%s AND expected_match_id=%s AND generation=%s AND kind='recover' AND state IN ('queued','waiting_source','retryable','needs_review') AND dedupe_key LIKE %s AND dedupe_key<>ALL(%s::text[])", (broadcast["id"], match["id"], job["generation"], f"recover:{source['id']}:%", active_recovery))

    def pending_coverage(self, job, source, segment, connection=None, recovery_only=False):
        query = """SELECT id FROM pipeline.jobs WHERE broadcast_id=%s AND generation=%s AND kind IN ('recover','live','probe_archive','reconcile')
                   AND state IN ('queued','running','waiting_source','retryable') AND failure_count<max_attempts
                   AND (NOT %s OR (kind='recover' AND dedupe_key<>%s))
                   AND ((dedupe_key=%s AND (NOT %s OR COALESCE((payload->>'checkpoint')::double precision,(payload->>'start')::double precision,0)<=%s))
                   OR (kind='recover' AND source_id=%s AND dedupe_key<>%s AND (expected_match_id=%s OR expected_match_id IS NULL) AND (payload->>'start')::double precision<=%s AND (payload->>'end')::double precision>=%s
                       AND NOT (dedupe_key LIKE %s AND %s AND COALESCE((payload->>'checkpoint')::double precision,(payload->>'start')::double precision,0)>%s))
                   OR (kind<>'recover' AND source_id=%s AND NOT %s)) LIMIT 1"""
        parameters = (job["broadcast_id"], job["generation"], recovery_only, f"archive-backfill:{source['id']}", f"archive-backfill:{source['id']}", bool(segment["evidence"].get("closed")), segment["end_time"], source["id"], f"archive-backfill:{source['id']}", segment["expected_match_id"], segment["end_time"], segment["start_time"], f"archive-tail:{source['id']}:%", bool(segment["evidence"].get("closed")), segment["end_time"], source["id"], bool(segment["evidence"].get("closed")))
        return bool(connection.execute(query, parameters).fetchone() if connection else self.store.one(query, parameters))

    def validate(self, job):
        broadcast, source, _, _ = self.context(job)
        segment = self.store.one("SELECT * FROM pipeline.segments WHERE id=%s", (job["payload"]["segment_id"],))
        match = self.store.one("SELECT * FROM pipeline.expected_matches WHERE id=%s", (job["expected_match_id"],))
        findings = []
        for finding in segment["findings"] + validate_rounds(segment["rounds"], match["best_of"]):
            if finding not in findings:
                findings.append(finding)
        if findings:
            pending = all(finding["code"] in {"incomplete_map", "incomplete_series", "missing_rounds", "late_start"} for finding in findings) and self.pending_coverage(job, source, segment)
            persist_index(
                self.store,
                job,
                segment,
                source,
                match,
                segment["rounds"],
                "validating" if pending else "needs_review",
                self.config.shadow_for(broadcast),
                findings=findings,
            )
            if pending:
                raise WaitingWork("Incomplete canonical evidence; waiting for active archive coverage/recovery: " + str(findings))
            raise NeedsReview(str(findings))
        if match["completion"] != "completed":
            raise WaitingWork("Series completion has not been confirmed")
        if self.pending_coverage(job, source, segment, recovery_only=True):
            persist_index(self.store, job, segment, source, match, segment["rounds"], "validating", self.config.shadow_for(broadcast))
            raise WaitingWork("Canonical capture gaps overlap this match; waiting for targeted recovery")
        verified = source["metadata"].get("live_dvr_range")
        needs_seekability = not broadcast["seekable"] or (
            broadcast["state"] == "live" and (verified is None or (
                "playback_shift" not in verified
                or verified.get("revision") != source["revision"]
                or verified["start"] > max(0, min(item["start"] for item in segment["rounds"]) - 15)
                or verified["end"] < max(item["start"] for item in segment["rounds"])
            ))
        )
        if needs_seekability:
            persist_index(
                self.store,
                job,
                segment,
                source,
                match,
                segment["rounds"],
                "validating",
                self.config.shadow_for(broadcast),
            )
            if broadcast["state"] == "live":
                verified = self.probe_live_seekability(job, broadcast, source, segment)
            else:
                raise WaitingWork("Completed index is frozen; waiting for YouTube seekability")
        if broadcast["reconciled_revision"] == broadcast["archive_revision"] and broadcast["archive_revision"] > 0:
            alignment = self.store.one(
                "SELECT mapping FROM pipeline.alignments WHERE source_id=%s AND kind='reconciliation' AND canonical_revision=%s",
                (source["id"], broadcast["archive_revision"]),
            )
            if alignment:
                rounds = (
                    segment["rounds"]
                    if segment["evidence"].get("timeline") == "archive"
                    else reconcile_rounds(segment["rounds"], alignment["mapping"])
                )
                persist_index(
                    self.store,
                    job,
                    segment,
                    source,
                    match,
                    rounds,
                    "final",
                    self.config.shadow_for(broadcast),
                    broadcast["archive_revision"],
                    {"method": "archive_reconciled", "timeline": "archive", "source_id": str(source["id"])},
                )
                return
        rounds = segment["rounds"]
        provenance = None
        if segment["evidence"].get("timeline") != "archive":
            if (
                not verified or "playback_shift" not in verified
                or verified.get("revision") != source["revision"]
                or verified["start"] > min(item["start"] for item in rounds)
                or verified["end"] < max(item["start"] for item in rounds)
            ):
                raise WaitingWork("Live index is frozen; waiting for a verified YouTube playback clock or archive reconciliation")
            rounds = [{**item, "start": round(item["start"] + verified["playback_shift"], 3)} for item in rounds]
            provenance = {"source_id": str(source["id"]), "method": "live_presentation_clock",
                          "timeline": "live_playback", "clock": verified}
        persist_index(
            self.store, job, segment, source, match, rounds, "provisional", self.config.shadow_for(broadcast),
            provenance=provenance,
        )

    def probe_live_seekability(self, job, broadcast, source, segment):
        start = source["actual_start"] or broadcast["actual_start"]
        if not start:
            raise WaitingWork("Official start time is required to verify live DVR coverage")
        remote = self.youtube.resolve(source["external_id"], live=True)
        playlist = self.media.manifest(remote, start_sequence=0)
        lower = max(0, min(round["start"] for round in segment["rounds"]) - 15)
        upper = max(round["start"] for round in segment["rounds"])
        covered = lower
        anchors = []
        for item in playlist:
            if item["wall_time"] is None:
                continue
            base = (item["wall_time"] - start).total_seconds()
            end = base + item["duration"]
            if end <= lower or base > upper:
                continue
            if base > covered + 0.25:
                raise WaitingWork("YouTube DVR has a gap in the indexed playback range")
            anchors.append(item)
            covered = max(covered, end)
        if not anchors or covered <= upper:
            raise WaitingWork("Completed index is frozen; waiting for YouTube DVR range coverage")
        if len({item.get("group", 0) for item in anchors}) != 1:
            raise NeedsReview("YouTube DVR playback clock crosses a discontinuity; archive reconciliation is required")
        clock_anchors = []
        for index in sorted({0, len(anchors) // 2, len(anchors) - 1}):
            item = anchors[index]
            media_time = (item["wall_time"] - start).total_seconds()
            playback_time = self.media.live_presentation_time(remote, item)
            clock_anchors.append({"media_time": media_time, "playback_time": playback_time})
        shifts = [item["playback_time"] - item["media_time"] for item in clock_anchors]
        if max(shifts) - min(shifts) > 0.25:
            raise NeedsReview("YouTube DVR presentation-clock anchors disagree; archive reconciliation is required")
        verified = {"start": lower, "end": upper, "revision": source["revision"],
                    "playback_shift": round(statistics.median(shifts), 6), "anchors": clock_anchors}
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            changed = connection.execute(
                "UPDATE pipeline.sources SET metadata=metadata||%s WHERE id=%s AND revision=%s",
                (Jsonb({"live_dvr_range": verified}), source["id"], source["revision"]),
            ).rowcount
            if not changed:
                raise StaleAttempt("Canonical source changed during DVR verification")
            connection.execute(
                "UPDATE pipeline.broadcasts SET seekable=true,updated_at=now() WHERE id=%s",
                (broadcast["id"],),
            )
        LOG.info("live_dvr_verified broadcast_id=%s source_id=%s job_id=%s segment_id=%s", broadcast["id"], source["id"], job["id"], segment["id"])
        return verified

    def fingerprint_archive(self, job, remote=None):
        broadcast, source, _, _ = self.context(job)
        if broadcast["state"] != "archive_ready":
            raise WaitingWork("Broadcast ended but archive is not processable")
        remote = remote or self.youtube.resolve(source["external_id"], height=720, cached=True)
        remote["cache_revision"] = (source["revision"], broadcast["archive_revision"])
        revision = broadcast["archive_revision"]
        archive = self.store.fingerprints(source, "archive", revision)
        duration = float(remote.get("duration") or 0)
        if duration <= 0:
            raise WaitingSource("Archive duration is missing")
        covered = {frame["time"] for frame in archive["frames"]}
        missing = [point for point in range(0, math.ceil(duration), 2) if point not in covered]
        if missing:
            self.check_stop()
            start = missing[0]
            end = min(start + 2, duration)
            for point in missing[1:]:
                if point != end or point >= start + 120:
                    break
                end = min(point + 2, duration)
            frames = list(self.media.archive_fingerprints(remote, start, end, interval=2))
            if not frames:
                raise WaitingSource("Archive fingerprint window was empty")
            self.store.checkpoint(
                job,
                source,
                source["checkpoint_time"],
                source["detector_state"],
                [],
                frames,
                revision=revision,
                timeline="archive",
            )
            self.store.heartbeat(job, self.config.lease_seconds)
            if set(missing) - {frame["time"] for frame in frames}:
                raise ContinueJob(f"Archive fingerprints checkpointed range {start:.3f}-{end:.3f}s")

    def reconcile(self, job):
        broadcast, source, _, _ = self.context(job)
        if broadcast["state"] != "archive_ready":
            raise WaitingWork("Broadcast ended but archive is not processable")
        remote = self.youtube.resolve(source["external_id"], height=720, cached=True)
        revision = broadcast["archive_revision"]
        duration = float(remote.get("duration") or 0)
        if duration <= 0:
            raise WaitingSource("Archive duration is missing")
        live = self.store.fingerprints(source)
        alignment: dict[str, Any] | None = None
        if len(live["frames"]) >= 30:
            archive = self.store.fingerprints(source, "archive", revision)
            archive.update(duration=duration, interval=2)
            if archive["frames"] and max(frame["time"] for frame in archive["frames"]) >= max(frame["time"] for frame in live["frames"]) - min(frame["time"] for frame in live["frames"]):
                try:
                    alignment = piecewise_alignment(archive, live, maximum_residual=self.config.maximum_residual)
                except NeedsReview as error:
                    LOG.info("partial_archive_alignment_pending job_id=%s reason=%s", job["id"], error)
            if alignment is None:
                self.fingerprint_archive(job, remote)
        archive = self.store.fingerprints(source, "archive", revision)
        archive.update(duration=duration, interval=2)
        if len(live["frames"]) < 30:
            if self.config.archive_start_for(broadcast) >= duration:
                raise NeedsReview("Configured archive start must be before the end of the official broadcast")
            live_count = self.store.one(
                "SELECT count(*) AS count FROM pipeline.round_candidates WHERE source_id=%s AND accepted AND timeline='live'",
                (source["id"],),
            )["count"]
            if live_count:
                raise NeedsReview("Captured live rounds lack enough fingerprints for safe archive reconciliation")
            alignment = {
                "timelineScale": 1,
                "segments": [
                    {
                        "sourceStart": 0,
                        "sourceEnd": duration,
                        "offset": 0,
                        "canonicalStart": 0,
                        "canonicalEnd": duration,
                    }
                ],
                "anchors": len(archive["frames"]),
                "maximumResidual": 0,
                "direction": "archive_identity",
                "version": "canonical-archive-identity-v1",
            }
            self.store.enqueue(
                "recover",
                f"archive-backfill:{source['id']}",
                broadcast=broadcast["id"],
                source=source["id"],
                payload={"start": self.config.archive_start_for(broadcast), "end": duration, "stage": 2, "timeline": "archive"},
                priority=20,
            )
            self.store.enqueue(
                "fingerprint_archive",
                f"archive-fingerprints:{source['id']}:{revision}",
                broadcast=broadcast["id"],
                source=source["id"],
                priority=-1,
            )
        else:
            try:
                if alignment is None:
                    alignment = piecewise_alignment(archive, live, maximum_residual=self.config.maximum_residual)
            except (ValueError, NeedsReview) as error:
                segments = self.store.rows(
                    "SELECT * FROM pipeline.segments WHERE broadcast_id=%s AND generation=%s AND expected_match_id IS NOT NULL",
                    (broadcast["id"], job["generation"]),
                )
                for segment in segments:
                    self.store.enqueue(
                        "reconcile_segment",
                        f"reconcile-segment:{segment['id']}:{revision}",
                        broadcast=broadcast["id"],
                        source=source["id"],
                        match=segment["expected_match_id"],
                        payload={"segment_id": str(segment["id"]), "revision": revision},
                    )
                raise NeedsReview(
                    "Broadcast alignment needs review; independent segment reconciliation was scheduled: " + str(error)
                ) from error
        self.save_alignment(job, source, source, alignment, "reconciliation", revision)
        if len(live["frames"]) >= 30:
            self.store.enqueue("fingerprint_archive", f"archive-fingerprints:{source['id']}:{revision}",
                               broadcast=broadcast["id"], source=source["id"], priority=-1)
        if live["frames"]:
            covered_end = max(section["canonicalEnd"] for section in alignment["segments"])
            if duration - covered_end > 30:
                self.store.enqueue(
                    "recover", f"archive-tail:{source['id']}:{revision}",
                    broadcast=broadcast["id"], source=source["id"],
                    payload={"start": max(0, covered_end - 30), "end": duration, "stage": 2, "timeline": "archive"},
                    priority=20,
                )
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            connection.execute(
                "UPDATE pipeline.broadcasts SET reconciled_revision=%s,updated_at=now() WHERE id=%s AND archive_revision=%s",
                (revision, broadcast["id"], revision),
            )
        segments = self.store.rows(
            "SELECT * FROM pipeline.segments WHERE broadcast_id=%s AND generation=%s AND expected_match_id IS NOT NULL",
            (broadcast["id"], job["generation"]),
        )
        self.store.enqueue("segment", f"segment:{broadcast['id']}", broadcast=broadcast["id"])
        for segment in segments:
            self.store.enqueue(
                "finalize",
                f"finalize:{segment['id']}:{revision}",
                broadcast=broadcast["id"],
                source=source["id"],
                match=segment["expected_match_id"],
                payload={"segment_id": str(segment["id"]), "revision": revision},
            )
        for secondary in self.store.rows(
            "SELECT id FROM pipeline.sources WHERE broadcast_id=%s AND provider='twitch'", (broadcast["id"],)
        ):
            self.store.enqueue(
                "twitch_align",
                f"archive-twitch-alignment:{secondary['id']}:{revision}",
                broadcast=broadcast["id"],
                source=secondary["id"],
            )

    def save_alignment(self, job, source, canonical, mapping, kind, revision):
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            current = connection.execute("SELECT archive_revision,reconciled_revision FROM pipeline.broadcasts WHERE id=%s", (source["broadcast_id"],)).fetchone()
            sources = connection.execute("SELECT id,revision FROM pipeline.sources WHERE id=ANY(%s) FOR SHARE",
                                         ([source["id"], canonical["id"]],)).fetchall()
            revisions = {row["id"]: row["revision"] for row in sources}
            if (revisions.get(source["id"]) != source["revision"] or revisions.get(canonical["id"]) != canonical["revision"]
                    or current["reconciled_revision" if kind == "secondary" else "archive_revision"] != revision):
                raise WaitingWork("Alignment evidence changed while processing; rebuilding against the current revisions")
            connection.execute(
                """INSERT INTO pipeline.alignments(id,source_id,canonical_source_id,source_revision,canonical_revision,kind,mapping,diagnostics)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_id,canonical_source_id,source_revision,canonical_revision,kind)
                               DO UPDATE SET mapping=EXCLUDED.mapping,diagnostics=EXCLUDED.diagnostics,
                               revision=alignments.revision+CASE WHEN alignments.mapping IS DISTINCT FROM EXCLUDED.mapping THEN 1 ELSE 0 END""",
                (
                    identifier(),
                    source["id"],
                    canonical["id"],
                    source["revision"],
                    revision,
                    kind,
                    Jsonb(mapping),
                    Jsonb(
                        {
                            "anchors": mapping["anchors"],
                            "segments": mapping["segments"],
                            "maximumResidual": mapping["maximumResidual"],
                        }
                    ),
                ),
            )

    def finalize(self, job):
        broadcast, source, _, _ = self.context(job)
        revision = job["payload"]["revision"]
        if broadcast["archive_revision"] != revision or broadcast["reconciled_revision"] != revision:
            raise WaitingSource("Archive revision has not been reconciled")
        segment = self.store.one("SELECT * FROM pipeline.segments WHERE id=%s", (job["payload"]["segment_id"],))
        match = self.store.one("SELECT * FROM pipeline.expected_matches WHERE id=%s", (job["expected_match_id"],))
        if match["completion"] != "completed" or segment["findings"]:
            raise NeedsReview("Segment is incomplete or contains validation findings")
        alignment = self.store.one(
            "SELECT * FROM pipeline.alignments WHERE source_id=%s AND canonical_revision=%s AND kind='reconciliation'",
            (source["id"], revision),
        )
        rounds = (
            segment["rounds"]
            if segment["evidence"].get("timeline") == "archive"
            else reconcile_rounds(segment["rounds"], alignment["mapping"])
        )
        persist_index(
            self.store,
            job,
            segment,
            source,
            match,
            rounds,
            "final",
            self.config.shadow_for(broadcast),
            revision,
            {"method": "archive_reconciled", "timeline": "archive", "alignment_id": str(alignment["id"])},
        )

    def reconcile_segment(self, job):
        broadcast, source, _, _ = self.context(job)
        revision = job["payload"]["revision"]
        if broadcast["archive_revision"] != revision:
            raise NeedsReview("Segment reconciliation targets an older archive")
        segment = self.store.one("SELECT * FROM pipeline.segments WHERE id=%s", (job["payload"]["segment_id"],))
        match = self.store.one("SELECT * FROM pipeline.expected_matches WHERE id=%s", (job["expected_match_id"],))
        if match["completion"] != "completed" or segment["findings"]:
            raise NeedsReview("Segment sequence requires review before timeline reconciliation")
        archive = self.store.fingerprints(source, "archive", revision)
        archive["interval"] = 2
        live = self.store.fingerprints(source)
        live["frames"] = [
            frame for frame in live["frames"] if segment["start_time"] - 30 <= frame["time"] <= segment["end_time"] + 30
        ]
        if not live["frames"]:
            raise NeedsReview("Segment has no captured fingerprints")
        live["duration"] = live["frames"][-1]["time"] + 10
        mapping = piecewise_alignment(archive, live, maximum_residual=self.config.maximum_residual)
        rounds = reconcile_rounds(segment["rounds"], mapping)
        provenance = {
            "method": "segment_archive_reconciled",
            "timeline": "archive",
            "source_id": str(source["id"]),
            "source_revision": source["revision"],
            "archive_revision": revision,
            "mapping": mapping,
        }
        persist_index(
            self.store,
            job,
            segment,
            source,
            match,
            rounds,
            "final",
            self.config.shadow_for(broadcast),
            revision,
            provenance,
        )

    def recover(self, job):
        broadcast, canonical, matches, aliases = self.context(job)
        if canonical["role"] != "canonical":
            canonical = self.store.one(
                "SELECT * FROM pipeline.sources WHERE broadcast_id=%s AND role='canonical'", (broadcast["id"],)
            )
        if broadcast["state"] == "live" and job["payload"].get("timeline", "live") == "live":
            return self.recover_live(job, broadcast, canonical, aliases)
        if broadcast["state"] != "archive_ready":
            raise WaitingWork("Targeted recovery is waiting for the official archive")
        start, end = float(job["payload"]["start"]), float(job["payload"]["end"])
        if job["dedupe_key"] == f"archive-backfill:{canonical['id']}":
            start = max(start, self.config.archive_start_for(broadcast))
        if not math.isfinite(start + end) or not 0 <= start < end:
            raise ValueError("Invalid recovery window")
        stage = job["payload"].get("stage", 2)
        if job["payload"].get("timeline", "live") == "live":
            alignment = self.store.one(
                "SELECT mapping FROM pipeline.alignments WHERE source_id=%s AND kind='reconciliation' AND canonical_revision=%s",
                (canonical["id"], broadcast["archive_revision"]),
            )
            if not alignment:
                raise WaitingWork("Recovery range is waiting for archive reconciliation")
            start = (
                0
                if start < alignment["mapping"]["segments"][0]["sourceStart"]
                else mapped_time(start, alignment["mapping"])
            )
            end = mapped_time(min(end, alignment["mapping"]["segments"][-1]["sourceEnd"]), alignment["mapping"])
        if stage == 2:
            try:
                self.recover_official(job, broadcast, canonical, start, end, aliases)
                return
            except NeedsReview as error:
                LOG.warning("recovery_stage_failed job_id=%s stage=2 reason=%s", job["id"], error)
                stage = 3
        sources = self.store.rows(
            "SELECT * FROM pipeline.sources WHERE broadcast_id=%s AND role IN ('full_match','official_twitch') ORDER BY CASE role WHEN 'full_match' THEN 0 ELSE 1 END",
            (broadcast["id"],),
        )
        expected_match = job.get("expected_match_id")
        if expected_match is None:
            segments = self.store.rows("SELECT expected_match_id FROM pipeline.segments WHERE broadcast_id=%s AND generation=%s AND start_time<=%s AND end_time>=%s", (broadcast["id"], job["generation"], end, start))
            if len(segments) == 1:
                expected_match = segments[0]["expected_match_id"]
        sources = [source for source in sources if source["role"] != "full_match" or expected_match is not None and source["metadata"].get("expected_match_id") == str(expected_match)]
        failures = []
        waiting = False
        for source in sources:
            if stage == 4 and source["role"] == "full_match":
                continue
            try:
                self.recover_secondary(job, broadcast, canonical, source, start, end, aliases)
                return
            except (ValueError, NeedsReview, WaitingSource) as error:
                waiting = waiting or isinstance(error, WaitingSource)
                if isinstance(error, WaitingSource) and source["provider"] == "youtube":
                    self.youtube.invalidate(source["external_id"])
                failures.append({"source_id": str(source["id"]), "reason": str(error)})
                LOG.warning("recovery_source_failed job_id=%s source_id=%s reason=%s", job["id"], source["id"], error)
        if not sources:
            raise WaitingSource("Official recovery found no rounds; waiting for a registered fallback source")
        if waiting:
            raise WaitingSource("Recovery is waiting for temporarily unavailable fallback sources: " + str(failures))
        raise NeedsReview("Recovery exhausted verified sources: " + str(failures))

    def recover_live(self, job, broadcast, canonical, aliases):
        start, end = float(job["payload"]["start"]), float(job["payload"]["end"])
        if not math.isfinite(start + end) or not 0 <= start < end:
            raise ValueError("Invalid recovery window")
        origin = canonical["actual_start"] or broadcast["actual_start"]
        if not origin:
            raise WaitingWork("Live recovery requires the official capture clock")
        persisted = self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]
        checkpoint = float(persisted.get("live_checkpoint", start))
        previous = self.store.one(
            "SELECT * FROM pipeline.round_candidates WHERE source_id=%s AND source_revision=%s AND timeline='live' AND accepted AND media_time<%s ORDER BY media_time DESC LIMIT 1",
            (canonical["id"], canonical["revision"], start),
        )
        state = {}
        if previous:
            state = {"map_number": previous["evidence"]["broadcast_map"],
                     "previous": {"round": previous["round_number"], "start": previous["evidence"]["start"], "scores": previous["scores"]}}
        detector = CandidateDetector(persisted.get("live_detector_state", state))
        remote = self.youtube.resolve(canonical["external_id"], live=True, height=720)
        playlist = self.media.manifest(remote, start_sequence=persisted.get("live_media_sequence", 0))
        covered = checkpoint
        processed = 0
        for item in playlist:
            if item["wall_time"] is None:
                continue
            base = (item["wall_time"] - origin).total_seconds()
            if base + item["duration"] <= checkpoint or base >= end:
                continue
            if base > covered + 0.25:
                raise WaitingWork("Targeted range is outside continuous live DVR coverage; waiting for the archive")
            candidates = []
            for offset, frame in self.media.live_frames(remote, item):
                self.check_stop()
                time = round(base + offset, 3)
                if time < checkpoint or time >= end:
                    continue
                sample, extra = self.media.observation(frame, time, aliases)
                candidate = detector.observe(sample, (item["wall_time"] + timedelta(seconds=offset)).isoformat(), extra)
                candidate["evidence"]["provenance"] = {"method": "live_official_youtube", "recovery": "direct_live_dvr",
                                                       "source_id": str(canonical["id"]), "timeline": "live"}
                self.save_diagnostic(canonical, candidate, frame)
                candidates.extend(detector.confirmed_candidates)
                candidates.append(candidate)
            if not candidates:
                raise WaitingSource("Live DVR recovery returned no decodable frames in the requested range")
            self.store.checkpoint(job, canonical, canonical["checkpoint_time"], canonical["detector_state"], candidates, [])
            covered = min(end, base + item["duration"])
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                connection.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s",
                                   (Jsonb({"live_checkpoint": covered, "live_detector_state": detector.state(), "live_media_sequence": item.get("sequence", 0)}), job["id"]))
            self.store.heartbeat(job, self.config.lease_seconds)
            processed += 1
            if covered >= end or processed >= 18:
                break
        self.store.enqueue("segment", f"segment:{broadcast['id']}", broadcast=broadcast["id"])
        if covered < end:
            if not processed:
                raise WaitingWork("Targeted range is outside live DVR coverage; waiting for the archive")
            raise ContinueJob("Live DVR recovery checkpointed")

    def recover_official(self, job, broadcast, canonical, start, end, aliases):
        remote = None
        aliases_signature = hashlib.sha256(json.dumps(aliases, sort_keys=True).encode()).hexdigest()
        alignment = self.store.one(
            "SELECT mapping FROM pipeline.alignments WHERE source_id=%s AND kind='reconciliation' AND canonical_revision=%s",
            (canonical["id"], broadcast["archive_revision"]),
        )
        if not alignment:
            raise WaitingWork("Official recovery waits for archive timebase reconciliation")
        observations = self.store.rows(
            "SELECT * FROM pipeline.round_candidates WHERE source_id=%s AND source_revision=%s ORDER BY media_time",
            (canonical["id"], canonical["revision"]),
        )
        detected = self.redetect_canonical(observations, alignment["mapping"])
        required_rounds: set[tuple[int, int]] = set()
        if job["dedupe_key"] != f"archive-backfill:{canonical['id']}":
            ordered = sorted(detected, key=lambda value: value["evidence"]["start"])
            for left, right in zip(ordered, ordered[1:]):
                if (left["evidence"]["broadcast_map"] == right["evidence"]["broadcast_map"]
                        and left["evidence"]["start"] <= end and right["evidence"]["start"] >= start):
                    required_rounds.update((left["evidence"]["broadcast_map"], number)
                                           for number in range(left["round_number"] + 1, right["round_number"]))
        previous = max((value for value in detected if value["media_time"] < start),
                       key=lambda value: value["evidence"]["start"], default=None)
        state = {}
        if previous:
            state = {
                "map_number": previous["evidence"]["broadcast_map"],
                "previous": {
                    "round": previous["round_number"],
                    "start": previous["evidence"]["start"],
                    "scores": previous["scores"],
                },
            }
        detector = CandidateDetector(state)
        persisted = self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]
        checkpoint = persisted.get("checkpoint", start)
        recovered = 0
        position = max(start, float(checkpoint))
        if position > start:
            detector = CandidateDetector(persisted.get("detector_state", state))
        if job["dedupe_key"] == f"archive-backfill:{canonical['id']}" and persisted.get("detector_state", {}).get("version") != DETECTOR_VERSION:
            observations = self.store.rows(
                "SELECT * FROM pipeline.round_candidates WHERE source_id=%s AND source_revision=%s AND timeline='archive' AND media_time<%s ORDER BY media_time",
                (canonical["id"], canonical["revision"], position),
            )
            _, detectors = redetect_candidates(observations)
            detector = detectors.get("archive", detector)
        while position < end:
            self.check_stop()
            stop = min(position + 90, end)
            candidates, fingerprints = [], []
            adaptive = job["dedupe_key"].startswith(("archive-backfill:", "archive-tail:"))
            saved = self.store.rows("SELECT * FROM pipeline.round_candidates WHERE source_id=%s AND source_revision=%s AND timeline='archive' AND detector_version=%s AND media_time>=%s AND media_time<%s ORDER BY media_time,confidence DESC", (canonical["id"], canonical["revision"], DETECTOR_VERSION, position - .001, stop))
            cached: dict[int, dict] = {}
            for value in saved:
                evidence = value["evidence"]
                capture = evidence.get("capture", {})
                offset = round(value["media_time"] - position)
                if (0 <= offset < stop - position and abs(value["media_time"] - position - offset) <= .001
                        and capture.get("ocr_version") == OCR_VERSION
                        and capture.get("archive_revision") == broadcast["archive_revision"]
                        and capture.get("aliases_signature") == aliases_signature
                        and evidence.get("provenance", {}).get("method") == "direct_official_archive"
                        and "time" in evidence and (adaptive or capture.get("mode") == "dense"
                        and value["timer"] is not None and value["confidence"] >= .85
                        and (value["round_number"] is not None or not 85 <= value["timer"] <= 100))):
                    cached.setdefault(offset, value)
            if len(cached) != math.ceil(stop - position):
                if remote is None:
                    remote = self.youtube.resolve(canonical["external_id"], height=720, cached=True, minimum_height=720)
                    remote["cache_revision"] = (canonical["revision"], broadcast["archive_revision"])
                end = min(end, float(remote["duration"]))
                stop = min(stop, end)
                if stop <= position:
                    break
                cached = {offset: value for offset, value in cached.items() if offset < stop - position}
            covered = {row["media_time"] for row in self.store.rows("SELECT media_time FROM pipeline.fingerprints WHERE source_id=%s AND source_revision=%s AND timeline='archive' AND media_time>=%s AND media_time<%s", (canonical["id"], broadcast["archive_revision"], position, stop))}
            replay_frames = self.store.rows("SELECT media_time AS time,hashes->>'gameplayHash' AS \"gameplayHash\" FROM pipeline.fingerprints WHERE source_id=%s AND source_revision=%s AND timeline='archive' AND media_time>=%s AND media_time<%s", (canonical["id"], broadcast["archive_revision"], max(0, position - 3600), position - 30))
            def observations():
                offset = 0
                while offset < stop - position:
                    if offset in cached:
                        value = cached[offset]
                        yield observation_from_dict({**value["evidence"], "time": value["media_time"]}), value["evidence"], None
                        offset += 1
                    else:
                        following = offset + 1
                        while following < stop - position and following not in cached:
                            following += 1
                        yield from self.media.window(remote, position + offset, min(position + following, stop), aliases,
                                                     adaptive=adaptive, detector=detector, replay_frames=replay_frames,
                                                     stream_id=job["id"])
                        offset = following
            if cached:
                LOG.info("archive_ocr_reused job_id=%s start=%.3f end=%.3f samples=%s", job["id"], position, stop, len(cached))
            for sample, extra, frame in observations():
                candidate = detector.observe(
                    sample,
                    (broadcast["actual_start"] + timedelta(seconds=sample.time)).isoformat()
                    if broadcast["actual_start"]
                    else None,
                    extra,
                )
                candidate["evidence"]["provenance"] = {
                    "method": "direct_official_archive",
                    "source_id": str(canonical["id"]),
                    "timeline": "archive",
                    "revision": broadcast["archive_revision"],
                }
                reused = cached.get(round(sample.time - position)) if frame is None else None
                candidate["evidence"]["capture"] = reused["evidence"]["capture"] if reused else {"ocr_version": OCR_VERSION,
                                                       "archive_revision": broadcast["archive_revision"],
                                                       "aliases_signature": aliases_signature,
                                                       "mode": "adaptive" if adaptive else "dense"}
                if frame is not None:
                    self.save_diagnostic(canonical, candidate, frame)
                else:
                    candidate["diagnostic_ref"] = reused.get("diagnostic_ref") if reused else None
                candidates.extend(detector.confirmed_candidates)
                candidates.append(candidate)
                recovered += int(candidate["accepted"]) + len(detector.confirmed_candidates)
                if frame is not None and int(sample.time) % 2 == 0 and sample.time not in covered:
                    fingerprints.append({"time": sample.time, **self.media.fingerprint(frame, compact=False)})
            self.store.checkpoint(
                job,
                canonical,
                canonical["checkpoint_time"],
                canonical["detector_state"],
                candidates,
                fingerprints,
                revision=broadcast["archive_revision"],
                timeline="archive",
            )
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                payload = {
                    **job["payload"],
                    "checkpoint": stop,
                    "detector_state": detector.state(),
                    "recovered": job["payload"].get("recovered", 0) + recovered,
                }
                connection.execute("UPDATE pipeline.jobs SET payload=%s WHERE id=%s", (Jsonb(payload), job["id"]))
                job["payload"] = payload
            position = stop
            self.store.heartbeat(job, self.config.lease_seconds)
            self.store.enqueue("segment", f"segment:{broadcast['id']}", broadcast=broadcast["id"])
            if position < end:
                raise ContinueJob(f"Official archive OCR checkpointed through {position:.3f}s")
        if not recovered and not job["payload"].get("recovered"):
            raise NeedsReview("Direct official OCR found no coherent rounds in the requested window")
        if required_rounds:
            observations = self.store.rows(
                "SELECT * FROM pipeline.round_candidates WHERE source_id=%s AND source_revision=%s ORDER BY media_time",
                (canonical["id"], canonical["revision"]))
            detected = self.redetect_canonical(observations, alignment["mapping"])
            found = {(value["evidence"]["broadcast_map"], value["round_number"]) for value in detected}
            if required_rounds - found:
                raise NeedsReview("Direct official OCR did not recover the missing rounds in the requested window")
        self.store.enqueue("segment", f"segment:{broadcast['id']}", broadcast=broadcast["id"])

    def recover_secondary(self, job, broadcast, canonical, source, start, end, aliases):
        import yt_dlp
        from storyboard_align import extract_storyboard

        media_id = source["external_id"] if source["provider"] == "youtube" else source["metadata"].get("vod_id")
        if not media_id:
            raise WaitingSource("Official Twitch recovery requires its eventual VOD ID")
        target = self.store.fingerprints(source, "archive")
        if not target["frames"]:
            target = extract_storyboard(
                ("https://www.youtube.com/watch?v=" if source["provider"] == "youtube" else "https://www.twitch.tv/videos/") + media_id,
                source["provider"], yt_dlp, precise_timestamps=source["provider"] == "youtube",
            )
            self.store.checkpoint(job, source, source["checkpoint_time"], source["detector_state"], [], target["frames"], timeline="archive")
        if len(target["frames"]) > 1:
            target["interval"] = statistics.median(right["time"] - left["time"] for left, right in zip(target["frames"], target["frames"][1:]))
        reference = self.store.fingerprints(canonical, "archive", broadcast["archive_revision"])
        reference["interval"] = 2
        alignment = piecewise_alignment(reference, target, maximum_residual=self.config.maximum_residual, local=True)
        self.save_alignment(job, source, canonical, alignment, "recovery", broadcast["archive_revision"])
        if source["provider"] == "youtube":
            remote = self.youtube.resolve(source["external_id"], height=720, cached=True)
            remote["cache_revision"] = (source["revision"],)
        else:
            from .twitch import resolve_twitch

            remote = resolve_twitch(media_id, live=False)
        recovered = 0
        canonical_candidates, _ = redetect_candidates(self.store.rows("SELECT * FROM pipeline.round_candidates WHERE source_id=%s AND source_revision=%s AND timeline='archive' ORDER BY media_time", (canonical["id"], canonical["revision"])))
        stored_candidates, _ = redetect_candidates(self.store.rows("SELECT * FROM pipeline.round_candidates WHERE source_id=%s AND source_revision=%s AND timeline='archive' ORDER BY media_time", (source["id"], source["revision"])))
        saved_progress = job["payload"].get("secondary_progress", {})
        if saved_progress.get("source_id") != str(source["id"]) or saved_progress.get("source_revision") != source["revision"] or saved_progress.get("archive_revision") != broadcast["archive_revision"] or saved_progress.get("alignment") != alignment:
            saved_progress = {"source_id": str(source["id"]), "source_revision": source["revision"], "archive_revision": broadcast["archive_revision"], "alignment": alignment, "sections": {}, "recovered": 0, "unmapped_rounds": []}
        for section in alignment["segments"]:
            lower = max(section["sourceStart"], (start - section["offset"]) / alignment["timelineScale"])
            upper = min(section["sourceEnd"], (end - section["offset"]) / alignment["timelineScale"])
            if lower >= upper:
                continue
            section_key = str(section["sourceStart"])
            progress = saved_progress["sections"].get(section_key, {})
            detector = CandidateDetector(progress.get("detector_state"))
            lower = max(lower, progress.get("checkpoint", lower))
            for position in range(math.ceil(lower), math.ceil(upper), 90):
                candidates: list[dict] = []
                stop = min(position + 90, upper)
                if source["checkpoint_time"] >= stop - 1:
                    values = [{**value, "evidence": dict(value["evidence"])} for value in stored_candidates if position <= value["media_time"] < stop]
                else:
                    values = []
                    for sample, extra, frame in self.media.window(remote, position, stop, aliases, stream_id=job["id"]):
                        self.check_stop()
                        value = detector.observe(sample, extra=extra)
                        values.extend(detector.confirmed_candidates)
                        values.append(value)
                for candidate in values:
                    self.check_stop()
                    if not candidate["accepted"]:
                        continue
                    try:
                        canonical_time = mapped_time(candidate["evidence"]["start"], alignment)
                    except NeedsReview as error:
                        finding = {"source_id": str(source["id"]), "source_time": candidate["evidence"]["start"], "reason": str(error)}
                        findings = job["payload"].setdefault("unmapped_rounds", [])
                        if finding not in findings:
                            findings.append(finding)
                        if finding not in saved_progress.setdefault("unmapped_rounds", []):
                            saved_progress["unmapped_rounds"].append(finding)
                        continue
                    neighbors = canonical_candidates + candidates
                    earlier = [row for row in neighbors if row["evidence"]["start"] < canonical_time]
                    if not earlier:
                        raise NeedsReview("Recovered rounds lack a canonical map identity anchor")
                    previous = max(earlier, key=lambda value: value["evidence"]["start"])
                    candidate["evidence"].update(
                        start=canonical_time,
                        broadcast_map=previous["evidence"]["broadcast_map"]
                        + int(candidate["round_number"] == 1 and previous["round_number"] >= 13),
                        provenance={
                            "method": "piecewise_secondary_recovery",
                            "source_id": str(source["id"]),
                            "alignment": alignment,
                            "source_time": candidate["media_time"],
                            "timeline": "archive",
                        },
                    )
                    candidate["media_time"] = canonical_time
                    candidate["detector_version"] += "-recovery-" + source["role"]
                    candidates.append(candidate)
                    recovered += 1
                self.store.checkpoint(
                    job,
                    canonical,
                    canonical["checkpoint_time"],
                    canonical["detector_state"],
                    candidates,
                    [],
                    timeline="archive",
                )
                stop = min(position + 90, upper)
                saved_progress["sections"][section_key] = {"checkpoint": stop, "detector_state": detector.state()}
                saved_progress["recovered"] = saved_progress.get("recovered", 0) + len(candidates)
                with self.store.transaction() as connection:
                    self.store.guard(connection, job)
                    job["payload"] = {**job["payload"], "secondary_progress": saved_progress, "secondary_recovered": job["payload"].get("secondary_recovered", 0) + len(candidates)}
                    connection.execute("UPDATE pipeline.jobs SET payload=%s WHERE id=%s", (Jsonb(job["payload"]), job["id"]))
                self.store.heartbeat(job, self.config.lease_seconds)
                self.store.enqueue("segment", f"segment:{broadcast['id']}", broadcast=broadcast["id"])
                if stop < upper:
                    raise ContinueJob(f"Aligned recovery checkpointed through source {stop:.3f}s")
        if saved_progress.get("unmapped_rounds"):
            raise NeedsReview("Some recovered rounds lie outside verified visual anchors; successful ranges were retained")
        if not recovered and not saved_progress.get("recovered"):
            raise NeedsReview("Fallback did not produce verified canonical rounds")
        self.store.enqueue("segment", f"segment:{broadcast['id']}", broadcast=broadcast["id"])

    def validate_upload(self, job):
        broadcast, source, _, aliases = self.context(job)
        if source["role"] != "full_match":
            raise NeedsReview("Upload validation requires a registered full-match source")
        segment = self.store.one(
            "SELECT * FROM pipeline.segments WHERE expected_match_id=%s AND generation=%s ORDER BY start_time LIMIT 1",
            (job["expected_match_id"], job["generation"]),
        )
        if not segment:
            raise WaitingWork("Full-match validation is waiting for the detected broadcast segment")
        remote = self.youtube.resolve(source["external_id"], height=720, cached=True, minimum_height=720)
        remote["cache_revision"] = (source["revision"],)
        duration = float(remote.get("duration") or 0)
        if duration <= 0:
            raise WaitingSource("Full-match upload is not processable")
        detector = CandidateDetector(source["detector_state"])
        start = max(0, source["checkpoint_time"] + 1)
        while start < duration:
            self.check_stop()
            end = min(start + 90, duration)
            candidates = []
            for sample, extra, frame in self.media.window(remote, start, end, aliases, compact=True, adaptive=True,
                                                         detector=detector, stream_id=job["id"]):
                candidate = detector.observe(sample, extra=extra)
                self.save_diagnostic(source, candidate, frame)
                candidates.extend(detector.confirmed_candidates)
                candidates.append(candidate)
            self.store.checkpoint(
                job, source, source["checkpoint_time"], source["detector_state"], candidates, [], timeline="archive"
            )
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                connection.execute(
                    "UPDATE pipeline.sources SET checkpoint_time=%s,detector_state=%s,updated_at=now() WHERE id=%s",
                    (end - 1, Jsonb(detector.state()), source["id"]),
                )
            start = end
            self.store.heartbeat(job, self.config.lease_seconds)
            if start < duration:
                raise ContinueJob(f"Full-match OCR checkpointed through {start:.3f}s")
        candidates = self.store.rows(
            "SELECT * FROM pipeline.round_candidates WHERE source_id=%s ORDER BY media_time",
            (source["id"],),
        )
        candidates, _ = redetect_candidates(candidates)
        upload = [
            {
                "map": value["evidence"]["broadcast_map"],
                "round": value["round_number"],
                "start": value["evidence"]["start"],
                "confidence": value["confidence"],
                "scores": value["scores"],
            }
            for value in candidates
        ]
        match = self.store.one("SELECT * FROM pipeline.expected_matches WHERE id=%s", (job["expected_match_id"],))
        findings = validate_rounds(upload, match["best_of"])
        segment = self.store.one(
            "SELECT * FROM pipeline.segments WHERE expected_match_id=%s AND generation=%s ORDER BY start_time LIMIT 1",
            (job["expected_match_id"], job["generation"]),
        )
        if not segment:
            raise WaitingWork("Full-match validation is waiting for the detected broadcast segment")
        missing = {(value["map"], value["round"]) for value in upload} - {
            (value["map"], value["round"]) for value in segment["rounds"]
        }
        extra = {(value["map"], value["round"]) for value in segment["rounds"]} - {
            (value["map"], value["round"]) for value in upload
        }
        comparison = {
            "source_id": str(source["id"]),
            "missing_rounds": sorted(missing),
            "extra_rounds": sorted(extra),
            "method": "sequence_only",
            "segment_revision": segment["revision"],
            "status": "source_incomplete" if findings and not missing and all(finding["code"] == "incomplete_series" for finding in findings) else "source_needs_review" if findings else "contradiction" if missing or extra else "confirmed",
            "source_findings": findings,
            "verified_contradiction": bool(findings and segment["evidence"].get("full_match_validation", {}).get("segment_revision") == segment["revision"] and (segment["evidence"]["full_match_validation"].get("status") == "contradiction" or segment["evidence"]["full_match_validation"].get("verified_contradiction"))),
        }
        comparison["severity"] = "warning" if findings and not missing and not comparison["verified_contradiction"] else "blocking" if findings or missing or extra else "info"
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            changed = connection.execute(
                "UPDATE pipeline.segments SET evidence=evidence||%s,updated_at=now() WHERE id=%s AND revision=%s RETURNING id",
                (Jsonb({"full_match_validation": comparison}), segment["id"], segment["revision"]),
            ).fetchone()
            if not changed:
                raise WaitingWork("Canonical segment changed during upload comparison; retrying against the current round sequence")
        if findings:
            if not missing and not comparison["verified_contradiction"]:
                LOG.warning("upload_validation_warning job_id=%s source_id=%s rules=%s", job["id"], source["id"], sorted({finding["code"] for finding in findings}))
                return
            raise NeedsReview("Full-match OCR itself requires review: " + str(findings))
        if missing or extra:
            canonical = self.store.one("SELECT * FROM pipeline.sources WHERE broadcast_id=%s AND role='canonical'", (broadcast["id"],))
            if missing and not extra and self.pending_coverage(job, canonical, segment):
                raise WaitingWork("Full-match validation waits for canonical archive coverage: " + str(comparison))
            for map_number, number in missing:
                before = [item for item in segment["rounds"] if item["map"] == map_number and item["round"] < number]
                after = [item for item in segment["rounds"] if item["map"] == map_number and item["round"] > number]
                if before and after:
                    lower, upper = before[-1]["start"] - 10, after[0]["start"] + 15
                    self.store.enqueue(
                        "recover",
                        f"upload-gap:{source['id']}:{map_number}:{number}",
                        broadcast=broadcast["id"],
                        source=self.store.one(
                            "SELECT id FROM pipeline.sources WHERE broadcast_id=%s AND role='canonical'",
                            (broadcast["id"],),
                        )["id"],
                        match=job["expected_match_id"],
                        payload={
                            "start": max(0, lower),
                            "end": upper,
                            "stage": 2,
                            "timeline": segment["evidence"].get("timeline", "live"),
                        },
                        priority=20,
                    )
            if missing and not extra and self.pending_coverage(job, canonical, segment):
                raise WaitingWork("Full-match validation waits for targeted canonical recovery: " + str(comparison))
            raise NeedsReview("Full-match sequence disagrees with the broadcast segment: " + str(comparison))
