import logging
import hashlib
import math
import re
from contextlib import nullcontext
from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb
from auto_publish import tournament_metadata

from .detection import DETECTOR_VERSION
from .errors import NeedsReview, StaleAttempt, WaitingWork
from .store import identifier, json_bytes, stable_id
from .timeline import validate_rounds


LOG = logging.getLogger(__name__)


def watchparty_contract(canonical, secondary, match, index, mapping, alignment_version, playback_shift=0):
    vod_id = secondary["metadata"].get("vod_id", "")
    if secondary["role"] != "watch_party" or secondary["provider"] != "twitch" or not re.fullmatch(r"[0-9]{6,20}", vod_id):
        raise NeedsReview("Watch-party publication requires a verified Twitch VOD")
    scale = mapping["timelineScale"]
    if not math.isfinite(scale) or not .95 <= scale <= 1.05:
        raise NeedsReview("Watch-party alignment has an unsupported timeline scale")
    rounds: list[dict] = []
    for value in index["rounds"]:
        start = value["start"] - playback_shift
        sections = [section for section in mapping["segments"]
                    if section["canonicalStart"] <= start - 5 * scale and section["canonicalEnd"] >= start + 2 * scale]
        if len(sections) != 1:
            raise NeedsReview("Watch-party round jump lies outside continuous verified visual coverage")
        section = sections[0]
        if section["anchors"] < 5 or section["maximumResidual"] > 2:
            raise NeedsReview("Watch-party round jump lacks precise visual anchors")
        target = round((start - section["offset"]) / scale, 3)
        if target < 5 or not math.isfinite(target) or rounds and target <= rounds[-1]["start"]:
            raise NeedsReview("Watch-party round sequence has invalid source timing")
        rounds.append({"map": value["map"], "round": value["round"], "start": target})
    return {
        "schemaVersion": 2, "provider": "twitch", "sourceId": vod_id,
        "label": match["team_a"] + " vs " + match["team_b"], "leadSeconds": 5,
        "detector": DETECTOR_VERSION + "+canonical-secondary", "pipelineState": index["state"],
        "pipelineVersion": index["version"], "sourceRevision": secondary["revision"],
        "alignmentVersion": alignment_version, "derivedFromCanonical": True, "rounds": rounds,
        "canonical": {"sourceId": canonical["external_id"], "matchIndexId": str(index["id"]),
                      "archiveRevision": index["archive_revision"], "rounds": canonical_contract(canonical, match, index)["rounds"]},
        "alignment": {"source": "youtube:" + canonical["external_id"], "timelineScale": scale, "strictCoverage": True,
                      "segments": [{"targetStart": section["sourceStart"], "targetEnd": section["sourceEnd"],
                                    "offset": section["offset"] + playback_shift} for section in mapping["segments"]]},
    }


def canonical_contract(source, match, index):
    if (
        source["role"] != "canonical"
        or source["provider"] != "youtube"
        or source["broadcast_id"] != index["broadcast_id"]
    ):
        raise NeedsReview("Only the official YouTube broadcast can own published timestamps")
    return {
        "schemaVersion": 2,
        "provider": "youtube",
        "sourceId": source["external_id"],
        "label": match["team_a"] + " vs " + match["team_b"],
        "leadSeconds": 5,
        "detector": DETECTOR_VERSION,
        "pipelineState": index["state"],
        "pipelineVersion": index["version"],
        "rounds": [{"map": item["map"], "round": item["round"], "start": item["start"]} for item in index["rounds"]],
    }


def persist_index(
    store, job, segment, source, match, rounds, state, shadow, revision=0, provenance=None, findings=None
):
    if source["role"] != "canonical" or source["provider"] != "youtube":
        raise NeedsReview("Secondary sources cannot become canonical")
    if state in {"provisional", "final"}:
        errors = validate_rounds(rounds, match["best_of"])
        if errors:
            raise NeedsReview(str(errors))
    with store.transaction() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(87314402)")
        store.guard(connection, job)
        current_source = connection.execute(
            "SELECT * FROM pipeline.sources WHERE id=%s FOR UPDATE", (source["id"],)
        ).fetchone()
        current_segment = connection.execute(
            "SELECT * FROM pipeline.segments WHERE id=%s FOR UPDATE", (segment["id"],)
        ).fetchone()
        if current_segment["revision"] != segment["revision"] or current_segment["expected_match_id"] != match["id"]:
            raise StaleAttempt("Segment changed while validation was running")
        if current_source["revision"] != source["revision"] or current_source["broadcast_id"] != job["broadcast_id"]:
            raise StaleAttempt("Canonical source changed")
        broadcast = connection.execute(
            "SELECT * FROM pipeline.broadcasts WHERE id=%s", (job["broadcast_id"],)
        ).fetchone()
        if source["external_id"] != broadcast["youtube_id"]:
            raise NeedsReview("Canonical source does not match the official broadcast")
        if (
            state in {"provisional", "final"}
            and not segment["evidence"].get("closed")
            and connection.execute(
                "SELECT id FROM pipeline.jobs WHERE dedupe_key=%s AND generation=%s AND state<>'succeeded'",
                (f"archive-backfill:{source['id']}", job["generation"]),
            ).fetchone()
        ):
            raise WaitingWork("Archive backfill has not reached the end of this match segment")
        latest = connection.execute(
            "SELECT * FROM pipeline.match_indexes WHERE expected_match_id=%s AND generation=%s ORDER BY version DESC LIMIT 1",
            (match["id"], job["generation"]),
        ).fetchone()
        if latest and latest["state"] == "final" and state != "final":
            return latest
        if state == "final" and (revision != broadcast["archive_revision"] or not revision):
            raise StaleAttempt("Archive revision changed")
        if state == "final":
            connection.execute("""UPDATE pipeline.jobs SET state='unsupported',failure_kind='superseded',
                last_error='Superseded by a newer validated final canonical index',updated_at=now()
                WHERE expected_match_id=%s AND generation=%s AND state='needs_review'
                AND kind IN ('validate','reconcile_segment','finalize','recover')
                AND (source_id=%s OR source_id IS NULL) AND id<>%s
                AND created_at<(SELECT created_at FROM pipeline.jobs WHERE id=%s)""",
                (match["id"], job["generation"], source["id"], job["id"], job["id"]))
        provenance = provenance or {
            "source_id": str(source["id"]),
            "method": "direct_official_youtube",
            "timeline": "live",
        }
        if (
            latest
            and latest["rounds"] == rounds
            and latest["state"] == state
            and latest["archive_revision"] == revision
            and latest["shadow"] == shadow
        ):
            return latest
        row = connection.execute(
            """INSERT INTO pipeline.match_indexes(id,broadcast_id,expected_match_id,segment_id,canonical_source_id,generation,version,archive_revision,state,shadow,rounds,provenance,findings,segment_revision)
                                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                identifier(),
                job["broadcast_id"],
                match["id"],
                segment["id"],
                source["id"],
                job["generation"],
                latest["version"] + 1 if latest else 1,
                revision,
                state,
                shadow,
                Jsonb(rounds),
                Jsonb(provenance),
                Jsonb(findings or []),
                segment["revision"],
            ),
        ).fetchone()
        connection.execute(
            "UPDATE pipeline.segments SET state=%s,updated_at=now() WHERE id=%s",
            (state if state != "validating" else "validating", segment["id"]),
        )
        LOG.info(
            "index_version match_index_id=%s expected_match_id=%s broadcast_id=%s state=%s version=%s shadow=%s",
            row["id"],
            match["id"],
            job["broadcast_id"],
            state,
            row["version"],
            shadow,
        )
        return row


def export_snapshot(store, storage, shadow=True, approved_broadcasts=None, connection=None):
    with (nullcontext(connection) if connection is not None else store.transaction()) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(87314402)")
        rows = connection.execute(
            """SELECT DISTINCT ON (expected_match_id) * FROM pipeline.match_indexes
                                  WHERE shadow=%s
                                  ORDER BY expected_match_id,generation DESC,version DESC""",
            (shadow,),
        ).fetchall()
        catalog: dict[str, Any] = {"version": 2, "videos": [], "withheld": []}
        for index in rows:
            source = connection.execute(
                "SELECT * FROM pipeline.sources WHERE id=%s", (index["canonical_source_id"],)
            ).fetchone()
            match = connection.execute(
                "SELECT * FROM pipeline.expected_matches WHERE id=%s", (index["expected_match_id"],)
            ).fetchone()
            broadcast = connection.execute(
                "SELECT * FROM pipeline.broadcasts WHERE id=%s", (index["broadcast_id"],)
            ).fetchone()
            if index["generation"] != broadcast["generation"]:
                continue
            if approved_broadcasts is not None and broadcast["youtube_id"] not in approved_broadcasts:
                continue
            if index["state"] not in {"provisional", "final"}:
                catalog["withheld"].append({"expectedMatchId": str(index["expected_match_id"]),
                                            "pipelineGeneration": index["generation"], "pipelineVersion": index["version"]})
                continue
            if not shadow:
                segment = connection.execute("SELECT state,revision,evidence FROM pipeline.segments WHERE id=%s", (index["segment_id"],)).fetchone()
                contradiction = connection.execute("SELECT id FROM pipeline.jobs WHERE broadcast_id=%s AND generation=%s AND expected_match_id=%s AND kind='validate_upload' AND state='needs_review' LIMIT 1", (broadcast["id"], broadcast["generation"], index["expected_match_id"])).fetchone()
                comparison = segment["evidence"].get("full_match_validation", {})
                source_only_failure = not comparison.get("verified_contradiction") and (comparison.get("status") == "source_needs_review" or (comparison.get("status") == "source_incomplete" and not comparison.get("missing_rounds")))
                if segment["state"] not in {"provisional", "final"} or segment["revision"] != index["segment_revision"] or (not source_only_failure and (contradiction or comparison.get("missing_rounds") or comparison.get("extra_rounds"))):
                    catalog["withheld"].append({"expectedMatchId": str(index["expected_match_id"]),
                                                "pipelineGeneration": index["generation"], "pipelineVersion": index["version"]})
                    continue
            if not broadcast["actual_start"]:
                catalog["withheld"].append({"expectedMatchId": str(index["expected_match_id"]),
                                            "pipelineGeneration": index["generation"], "pipelineVersion": index["version"]})
                LOG.warning("export_match_withheld broadcast_id=%s expected_match_id=%s match_index_id=%s reason=missing_broadcast_start", broadcast["id"], match["id"], index["id"])
                continue
            key = stable_id("catalog", match["id"]).hex[:16]
            prefix = "shadow" if shadow else "release"
            contract = canonical_contract(source, match, index)
            playback_shift = (
                index["provenance"]["clock"]["playback_shift"]
                if index["provenance"].get("method") == "live_presentation_clock" else 0
            )
            chat_source = connection.execute(
                """SELECT s.*,a.mapping FROM pipeline.alignments a JOIN pipeline.sources s ON s.id=a.source_id
                                             WHERE a.canonical_source_id=%s AND a.kind='secondary' AND a.canonical_revision=%s AND a.source_revision=s.revision AND s.provider='twitch' AND s.role='official_twitch'
                                             AND EXISTS (SELECT 1 FROM pipeline.chat_messages m WHERE m.source_id=s.id)
                                             ORDER BY a.created_at DESC LIMIT 1""",
                (source["id"], index["archive_revision"]),
            ).fetchone()
            chat_fields = {}
            if chat_source:
                from .chat import chat_alignment, compact_chat

                with connection.cursor(name="official_chat_export") as cursor:
                    cursor.execute("SELECT * FROM pipeline.chat_messages WHERE source_id=%s ORDER BY media_time,message_id", (chat_source["id"],))
                    chat = compact_chat(chat_source, cursor)
                if chat["messages"]:
                    chat_source_id = chat_source["metadata"].get("vod_id") or chat_source["external_id"]
                    chat_file = f"twitch-{chat_source_id}.json"
                    storage.put(f"{prefix}/chats/{chat_file}", json_bytes(chat))
                    mapping = chat_source["mapping"]
                    if playback_shift:
                        mapping = {**mapping, "segments": [
                            {**section, "offset": section["offset"] + playback_shift,
                             "canonicalStart": section["canonicalStart"] + playback_shift,
                             "canonicalEnd": section["canonicalEnd"] + playback_shift}
                            for section in mapping["segments"]
                        ]}
                    contract["alignment"] = chat_alignment(mapping, chat_source_id)
                    chat_fields = {"chat": "/chats/" + chat_file, "chatSourceId": chat_source_id}
            digest = hashlib.sha256(json_bytes(contract)).hexdigest()[:12]
            filename = f"youtube-{source['external_id']}-{key}-v{index['version']}-{digest}.json"
            storage.put(f"{prefix}/indexes/{filename}", json_bytes(contract))
            catalog["videos"].append(
                {
                    "provider": "youtube",
                    "sourceId": source["external_id"],
                    "catalogId": f"youtube:{key}:{source['external_id']}",
                    "title": match["team_a"] + " vs " + match["team_b"],
                    "event": match["event"] + " | " + match["stage"],
                    "label": "Full match",
                    "index": "/indexes/" + filename,
                    "playedAt": (
                        broadcast["actual_start"] + timedelta(seconds=index["rounds"][0]["start"] - playback_shift)
                    ).isoformat(),
                    "pipelineState": index["state"],
                    "expectedMatchId": str(match["id"]),
                    "pipelineGeneration": index["generation"],
                    "pipelineVersion": index["version"],
                    **tournament_metadata(match["event"], match["team_a"] + " vs " + match["team_b"]),
                    **chat_fields,
                }
            )
        official = {entry["expectedMatchId"]: entry for entry in catalog["videos"]}
        for index in rows:
            parent = official.get(str(index["expected_match_id"]))
            if not parent:
                continue
            canonical = connection.execute("SELECT * FROM pipeline.sources WHERE id=%s", (index["canonical_source_id"],)).fetchone()
            match = connection.execute("SELECT * FROM pipeline.expected_matches WHERE id=%s", (index["expected_match_id"],)).fetchone()
            for secondary in connection.execute(
                """SELECT s.*,a.mapping,a.revision AS alignment_version FROM pipeline.sources s
                   LEFT JOIN pipeline.alignments a ON a.source_id=s.id AND a.canonical_source_id=%s
                   AND a.source_revision=s.revision AND a.canonical_revision=%s AND a.kind='secondary'
                   WHERE s.broadcast_id=%s AND s.provider='twitch' AND s.role='watch_party' AND s.metadata ? 'vod_id'""",
                (canonical["id"], index["archive_revision"], index["broadcast_id"])).fetchall():
                vod_id = secondary["metadata"]["vod_id"]
                key = stable_id("catalog", match["id"]).hex[:16]
                identity = f"twitch:{vod_id}:{key}"
                version = {"expectedMatchId": str(match["id"]), "catalogId": identity, "provider": "twitch", "sourceId": vod_id,
                           "pipelineGeneration": index["generation"], "pipelineVersion": index["version"],
                           "pipelineSourceRevision": secondary["revision"], "pipelineAlignmentVersion": secondary["alignment_version"] or 0}
                state, finding = "ready", None
                try:
                    if not secondary["mapping"]:
                        raise WaitingWork("Watch-party is waiting for current canonical alignment")
                    shift = index["provenance"]["clock"]["playback_shift"] if index["provenance"].get("method") == "live_presentation_clock" else 0
                    contract = watchparty_contract(canonical, secondary, match, index, secondary["mapping"], secondary["alignment_version"], shift)
                except (NeedsReview, WaitingWork) as error:
                    pending = connection.execute("SELECT id FROM pipeline.jobs WHERE source_id=%s AND kind IN ('twitch_vod','twitch_live','twitch_align') AND state IN ('queued','running','waiting_source','retryable') LIMIT 1", (secondary["id"],)).fetchone()
                    state = "waiting_alignment" if pending or isinstance(error, WaitingWork) else "needs_review"
                    finding = str(error)
                    catalog["withheld"].append({**version, "reason": state})
                if not shadow:
                    connection.execute(
                        """INSERT INTO pipeline.watchparty_publications(source_id,expected_match_id,canonical_index_id,state,findings)
                           VALUES (%s,%s,%s,%s,%s) ON CONFLICT(source_id,expected_match_id) DO UPDATE SET
                           canonical_index_id=EXCLUDED.canonical_index_id,state=EXCLUDED.state,findings=EXCLUDED.findings,updated_at=now()""",
                        (secondary["id"], match["id"], index["id"], state, Jsonb([finding] if finding else [])))
                if finding:
                    continue
                prefix = "shadow" if shadow else "release"
                filename = f"twitch-{vod_id}-{key}-v{index['version']}-{hashlib.sha256(json_bytes(contract)).hexdigest()[:12]}.json"
                storage.put(f"{prefix}/indexes/{filename}", json_bytes(contract))
                from .chat import compact_chat

                with connection.cursor(name="watchparty_chat_export") as cursor:
                    cursor.execute("SELECT * FROM pipeline.chat_messages WHERE source_id=%s ORDER BY media_time,message_id", (secondary["id"],))
                    chat = compact_chat(secondary, cursor)
                chat_fields = {}
                if chat["messages"]:
                    storage.put(f"{prefix}/chats/twitch-{vod_id}.json", json_bytes(chat))
                    chat_fields = {"chat": f"/chats/twitch-{vod_id}.json", "chatSourceId": vod_id}
                catalog["videos"].append({**{name: value for name, value in parent.items() if name not in {"chat", "chatSourceId"}},
                                          **version, "kind": "watch-party", "creator": "FNS" if secondary["metadata"].get("login") == "gofns" else secondary["metadata"].get("login", "Watch party"),
                                          "label": "Watch party", "index": "/indexes/" + filename, **chat_fields})
        if not shadow:
            blocked = [entry["expectedMatchId"] for entry in catalog["withheld"] if entry.get("provider") != "twitch"]
            connection.execute("UPDATE pipeline.watchparty_publications SET state='needs_review',findings=%s,updated_at=now() WHERE expected_match_id=ANY(%s::uuid[])", (Jsonb(["Canonical match is not currently publishable"]), blocked))
        catalog["videos"].sort(key=lambda item: item["playedAt"], reverse=True)
        storage.put(("shadow" if shadow else "release") + "/catalog.json", json_bytes(catalog))
        return catalog
