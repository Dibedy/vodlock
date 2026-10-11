import json
import hashlib
import logging
import math
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from psycopg.types.json import Jsonb

from .errors import ContinueJob, NeedsReview, WaitingSource, WaitingWork
from .detection import observation_from_dict
from .media import MediaAnalysis
from .schedule import fetch_bytes
from .store import stable_id
from .timeline import piecewise_alignment
from .timeline import scoreboard_alignment, scoreboard_section
from .youtube import timestamp


LOG = logging.getLogger(__name__)


def resolve_twitch(value, live=True):
    import yt_dlp

    url = "https://www.twitch.tv/" + value if live else "https://www.twitch.tv/videos/" + value
    options = {
        "quiet": True,
        "socket_timeout": 20,
        "retries": 1,
        "format": "best[protocol^=m3u8][height<=720]/best[height<=720]",
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
    if not info or not info.get("url"):
        raise WaitingSource("Twitch source is not processable yet")
    return {"url": info["url"], "duration": info.get("duration"), "headers": info.get("http_headers", {}),
            "actual_start": timestamp(info.get("timestamp"))}


def resolve_channels(channels, requester=fetch_bytes):
    missing = [channel for channel in channels if not channel.get("broadcaster_user_id")]
    if not missing:
        return channels
    token, client = os.environ.get("TWITCH_USER_TOKEN"), os.environ.get("TWITCH_CLIENT_ID")
    if not token or not client:
        raise WaitingWork("Twitch channel resolution requires local TWITCH_USER_TOKEN and TWITCH_CLIENT_ID")
    query = urlencode([("login", channel["login"]) for channel in missing])
    values = json.loads(requester("https://api.twitch.tv/helix/users?" + query,
                                  {"Authorization": "Bearer " + token, "Client-Id": client}))["data"]
    users = {value["login"].lower(): str(value["id"]) for value in values}
    if any(channel["login"].lower() not in users for channel in missing):
        raise WaitingSource("Twitch did not resolve every configured channel login")
    return [{**channel, "broadcaster_user_id": channel.get("broadcaster_user_id") or users[channel["login"].lower()]}
            for channel in channels]


def ingest_event(store, envelope, channels):
    metadata, payload = envelope["metadata"], envelope["payload"]
    event = payload["event"]
    kind = payload["subscription"]["type"]
    channel = next(
        (value for value in channels if str(value["broadcaster_user_id"]) == str(event["broadcaster_user_id"])), None
    )
    if not channel:
        return
    with store.transaction() as connection:
        duplicate = connection.execute(
            """INSERT INTO pipeline.source_events(provider,event_id,event_type,payload)
                                       VALUES ('twitch',%s,%s,%s) ON CONFLICT DO NOTHING RETURNING event_id""",
            (metadata["message_id"], kind, Jsonb(payload)),
        ).fetchone()
        if not duplicate:
            return
        broadcasts = connection.execute(
            "SELECT * FROM pipeline.broadcasts WHERE channel_id=%s AND state IN ('live','ended_waiting_archive') ORDER BY day DESC LIMIT 2",
            (channel["official_youtube_channel_id"],),
        ).fetchall()
        if len(broadcasts) != 1:
            store.enqueue(
                "twitch_event",
                "twitch-event:" + metadata["message_id"],
                payload={"envelope": envelope, "channel": channel},
                connection=connection,
            )
            return
        if (
            kind != "stream.online"
            and not connection.execute(
                "SELECT id FROM pipeline.sources WHERE broadcast_id=%s AND provider='twitch' AND metadata->>'broadcaster_user_id'=%s LIMIT 1",
                (broadcasts[0]["id"], str(channel["broadcaster_user_id"])),
            ).fetchone()
        ):
            store.enqueue(
                "twitch_event",
                "twitch-event:" + metadata["message_id"],
                payload={"envelope": envelope, "channel": channel},
                connection=connection,
            )
            return
        apply_event(store, connection, envelope, channel, broadcasts[0])


def apply_event(store, connection, envelope, channel, broadcast):
    event = envelope["payload"]["event"]
    kind = envelope["payload"]["subscription"]["type"]
    broadcaster = str(channel["broadcaster_user_id"])
    if kind == "stream.online":
        source_id = stable_id("source", broadcast["id"], "twitch", event["id"])
        start = timestamp(event["started_at"])
        connection.execute(
            """INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,state,actual_start,metadata)
                           VALUES (%s,%s,'twitch',%s,%s,'live',%s,%s) ON CONFLICT DO NOTHING""",
            (
                source_id,
                broadcast["id"],
                str(event["id"]),
                channel.get("role", "watch_party"),
                start,
                Jsonb({"login": channel["login"], "broadcaster_user_id": broadcaster}),
            ),
        )
        gap_end = max(0, (datetime.now(timezone.utc) - start).total_seconds())
        connection.execute(
            "INSERT INTO pipeline.chat_archives(source_id,gaps) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (source_id, Jsonb([[0, gap_end]] if gap_end else [])),
        )
        store.enqueue(
            "twitch_live",
            f"twitch_live:{source_id}",
            broadcast=broadcast["id"],
            source=source_id,
            priority=8,
            connection=connection,
        )
    else:
        sources = connection.execute(
            "SELECT * FROM pipeline.sources WHERE broadcast_id=%s AND provider='twitch' AND metadata->>'broadcaster_user_id'=%s ORDER BY actual_start DESC LIMIT 1",
            (broadcast["id"], broadcaster),
        ).fetchall()
        if not sources:
            raise WaitingSource("Twitch chat/lifecycle event is waiting for stream identity")
        source = sources[0]
        source_id = source["id"]
        if kind == "stream.offline":
            connection.execute("UPDATE pipeline.sources SET state='ended',updated_at=now() WHERE id=%s", (source_id,))
            connection.execute(
                "UPDATE pipeline.chat_archives SET state='waiting_vod',updated_at=now() WHERE source_id=%s",
                (source_id,),
            )
            store.enqueue(
                "twitch_align",
                f"twitch_align:{source_id}",
                broadcast=broadcast["id"],
                source=source_id,
                connection=connection,
            )
            store.enqueue(
                "chat_gaps",
                f"chat_gaps:{source_id}",
                broadcast=broadcast["id"],
                source=source_id,
                connection=connection,
            )
        elif kind == "channel.chat.message":
            wall = timestamp(envelope["metadata"]["message_timestamp"])
            offset = (wall - source["actual_start"]).total_seconds()
            if offset < 0:
                raise NeedsReview("Twitch chat timestamp precedes the stream")
            connection.execute(
                """INSERT INTO pipeline.chat_messages(source_id,message_id,media_time,wall_time,origin,message)
                               VALUES (%s,%s,%s,%s,'live',%s) ON CONFLICT DO NOTHING""",
                (
                    source_id,
                    event["message_id"],
                    offset,
                    wall,
                    Jsonb(
                        {
                            "user": event["chatter_user_name"],
                            "color": event.get("color", ""),
                            "message": event["message"],
                        }
                    ),
                ),
            )
            connection.execute(
                "UPDATE pipeline.chat_archives SET checkpoint_time=GREATEST(checkpoint_time,%s),updated_at=now() WHERE source_id=%s",
                (offset, source_id),
            )
    connection.execute(
        "UPDATE pipeline.source_events SET source_id=%s WHERE provider='twitch' AND event_id=%s",
        (source_id, envelope["metadata"]["message_id"]),
    )


class TwitchCapture:
    def __init__(self, store, config, processing, media=None):
        self.store = store
        self.config = config
        self.processing = processing
        self.media = media or MediaAnalysis()

    def discover_vods(self, job):
        import yt_dlp

        channel = job["payload"]["channel"]
        broadcasts = self.store.rows(
            """SELECT * FROM pipeline.broadcasts WHERE channel_id=%s AND actual_start IS NOT NULL
               AND state IN ('ended_waiting_archive','archive_ready')
               AND day>=current_date-2 ORDER BY actual_start""",
            (channel["official_youtube_channel_id"],))
        if not broadcasts:
            return
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                              "playlistend": 5, "socket_timeout": 20, "retries": 1}) as downloader:
            listing = downloader.extract_info("https://www.twitch.tv/" + channel["login"] +
                                              "/videos?filter=archives&sort=time", download=False)
        for entry in (listing or {}).get("entries", []):
            vod_id = str(entry.get("id", "")).removeprefix("v")
            if not vod_id.isdigit():
                continue
            known = self.store.one(
                "SELECT id FROM pipeline.sources WHERE provider='twitch' AND (external_id=%s OR metadata->>'vod_id'=%s) LIMIT 1",
                (vod_id, vod_id))
            if known:
                continue
            key = f"associate-twitch-vod:{channel['login']}:{vod_id}"
            if not self.store.one("SELECT id FROM pipeline.jobs WHERE dedupe_key=%s", (key,)):
                self.store.enqueue("associate_twitch_vod", key, payload={"channel": channel, "vod_id": vod_id}, priority=-40)

    def associate_vod(self, job):
        channel, vod_id = job["payload"]["channel"], job["payload"]["vod_id"]
        broadcasts = self.store.rows(
            "SELECT * FROM pipeline.broadcasts WHERE channel_id=%s AND actual_start IS NOT NULL AND day>=current_date-2",
            (channel["official_youtube_channel_id"],))
        remote = resolve_twitch(vod_id, live=False)
        start, duration = remote.get("actual_start"), float(remote.get("duration") or 0)
        if not start or not math.isfinite(duration) or duration <= 0:
            raise WaitingSource("Twitch VOD association is waiting for source timing")
        candidates = [broadcast for broadcast in broadcasts
                      if start <= broadcast["actual_start"] + timedelta(minutes=30)
                      and start + timedelta(seconds=duration) >= broadcast["actual_start"] + timedelta(minutes=30)
                      and abs((start - broadcast["actual_start"]).total_seconds()) <= 6 * 3600]
        if len(candidates) != 1:
            LOG.info("twitch_vod_unassigned job_id=%s vod_id=%s reason=ambiguous_broadcast_window", job["id"], vod_id)
            if candidates:
                raise NeedsReview("Twitch VOD overlaps multiple official broadcasts")
            return
        broadcast = candidates[0]
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            existing = connection.execute(
                """SELECT * FROM pipeline.sources WHERE broadcast_id=%s AND provider='twitch'
                   AND (external_id=%s OR metadata->>'vod_id'=%s OR
                        (metadata->>'login'=%s AND abs(extract(epoch FROM (actual_start-%s::timestamptz)))<=60)) FOR UPDATE""",
                (broadcast["id"], vod_id, vod_id, channel["login"], start)).fetchall()
            if len(existing) > 1:
                raise NeedsReview("Twitch VOD has multiple matching live sources")
            source_id = existing[0]["id"] if existing else stable_id("source", broadcast["id"], "twitch", vod_id)
            metadata = {"vod_id": vod_id, "login": channel["login"]}
            if existing:
                if existing[0]["role"] != channel["role"]:
                    raise NeedsReview("Twitch archive role conflicts with its existing source")
                connection.execute("UPDATE pipeline.sources SET metadata=metadata||%s,updated_at=now() WHERE id=%s",
                                   (Jsonb(metadata), source_id))
            else:
                inserted = connection.execute(
                    """INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,state,actual_start,metadata)
                       VALUES (%s,%s,'twitch',%s,%s,'ended',%s,%s)
                       ON CONFLICT(broadcast_id,provider,external_id) DO UPDATE SET metadata=sources.metadata||EXCLUDED.metadata
                       RETURNING id,role""",
                    (source_id, broadcast["id"], vod_id, channel["role"], start, Jsonb(metadata))).fetchone()
                if inserted["role"] != channel["role"]:
                    raise NeedsReview("Twitch archive role conflicts with its existing source")
                source_id = inserted["id"]
            connection.execute(
                """INSERT INTO pipeline.chat_archives(source_id,state,gaps) VALUES (%s,'waiting_vod',%s)
                   ON CONFLICT(source_id) DO UPDATE SET gaps=chat_archives.gaps||EXCLUDED.gaps
                   WHERE chat_archives.state<>'complete'""", (source_id, Jsonb([[0, duration]])))
            for kind in ("twitch_vod", "chat_gaps"):
                key = f"{kind}:{source_id}"
                previous = connection.execute("SELECT id,state,last_error FROM pipeline.jobs WHERE dedupe_key=%s", (key,)).fetchone()
                if not previous:
                    self.store.enqueue(kind, key, broadcast=broadcast["id"], source=source_id,
                                           priority=-30, connection=connection)
                elif kind == "chat_gaps" and previous["state"] in {"waiting_source", "needs_review"} and "VOD ID" in (previous["last_error"] or ""):
                    connection.execute("UPDATE pipeline.jobs SET state='queued',failure_count=0,last_error=NULL,available_at=now(),updated_at=now() WHERE id=%s", (previous["id"],))
            LOG.info("twitch_vod_associated job_id=%s broadcast_id=%s source_id=%s", job["id"], broadcast["id"], source_id)

    def vod(self, job):
        broadcast, source, _, _ = self.processing.context(job)
        vod_id = source["metadata"].get("vod_id")
        if not vod_id:
            raise WaitingWork("Twitch archive processing requires a verified VOD ID")
        remote = resolve_twitch(vod_id, live=False)
        duration = float(remote.get("duration") or 0)
        start = source["actual_start"] or remote.get("actual_start")
        if not start or not math.isfinite(duration) or duration <= 0:
            raise WaitingSource("Twitch archive timing metadata is not ready")
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            connection.execute("UPDATE pipeline.sources SET actual_start=COALESCE(actual_start,%s),state='ended',updated_at=now() WHERE id=%s",
                               (start, source["id"]))
            connection.execute("INSERT INTO pipeline.chat_archives(source_id,state,gaps) VALUES (%s,'waiting_vod',%s) ON CONFLICT DO NOTHING",
                               (source["id"], Jsonb([[0, duration]])))
            archive = connection.execute("SELECT state FROM pipeline.chat_archives WHERE source_id=%s", (source["id"],)).fetchone()
            if archive["state"] != "complete":
                self.store.enqueue("chat_gaps", f"chat_gaps:{source['id']}", broadcast=broadcast["id"], source=source["id"], connection=connection)
        if source["role"] == "watch_party" and self.seed_rounds(job, broadcast, source, remote, start):
            return
        position = float(job["payload"].get("checkpoint", 0))
        if position >= duration:
            self.store.enqueue("twitch_align", f"twitch_align:{source['id']}", broadcast=broadcast["id"], source=source["id"])
            return
        end = min(position + 120, duration)
        frames = list(self.media.archive_fingerprints(remote, position, end, interval=2))
        if not frames:
            raise WaitingSource("Twitch archive fingerprint window returned no frames")
        self.store.checkpoint(job, source, source["checkpoint_time"], source["detector_state"], [], frames, timeline="secondary")
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            connection.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s", (Jsonb({"checkpoint": end}), job["id"]))
            connection.execute("UPDATE pipeline.sources SET checkpoint_time=GREATEST(checkpoint_time,%s),updated_at=now() WHERE id=%s",
                               (end, source["id"]))
        if end < duration:
            raise ContinueJob("Twitch archive fingerprints checkpointed")
        self.store.enqueue("twitch_align", f"twitch_align:{source['id']}", broadcast=broadcast["id"], source=source["id"])

    def seed_rounds(self, job, broadcast, source, remote, source_start):
        progress = job["payload"].get("round_seed", {})
        revision = broadcast["archive_revision"]
        current_seed = progress.get("canonical_revision") == revision and progress.get("source_revision") == source["revision"]
        if current_seed and progress.get("state") == "fallback":
            return False
        if current_seed and progress.get("state") == "verified":
            return True
        if not revision or broadcast["reconciled_revision"] != revision:
            raise WaitingWork("Watch-party round search is waiting for the verified official archive")
        index = self.store.one("""SELECT i.* FROM pipeline.match_indexes i WHERE i.broadcast_id=%s
            AND i.generation=%s AND i.archive_revision=%s AND i.state IN ('provisional','final')
            ORDER BY (i.rounds->0->>'start')::double precision,version DESC LIMIT 1""",
            (broadcast["id"], broadcast["generation"], revision))
        if not index or not index["rounds"]:
            raise WaitingWork("Watch-party round search is waiting for ready official round timestamps")
        canonical = self.store.one("SELECT * FROM pipeline.sources WHERE broadcast_id=%s AND role='canonical'", (broadcast["id"],))
        if not canonical["actual_start"]:
            return False
        reference = self.store.fingerprints(canonical, "archive", revision)
        reference["interval"] = 2
        target = self.store.fingerprints(source, "secondary")
        target["interval"] = 2
        seed = {"canonical_revision": revision, "source_revision": source["revision"], "attempt": progress.get("attempt", 0) if current_seed else 0}
        try:
            alignment = piecewise_alignment(reference, target, maximum_distance=18,
                                            maximum_residual=self.config.maximum_residual, local=True)
        except NeedsReview:
            predicted = index["rounds"][0]["start"] + (canonical["actual_start"] - source_start).total_seconds()
            before, after = (60, 120) if seed["attempt"] == 0 else (180, 240)
            lower, upper = max(0, predicted - before), min(float(remote["duration"]), predicted + after)
            if upper > lower:
                frames = list(self.media.archive_fingerprints(remote, lower, upper, interval=2))
                if not frames:
                    raise WaitingSource("Watch-party seed search returned no decodable frames")
                self.store.checkpoint(job, source, source["checkpoint_time"], source["detector_state"], [], frames, timeline="secondary")
                target = self.store.fingerprints(source, "secondary")
                target["interval"] = 2
            seed["attempt"] += 1
            try:
                alignment = piecewise_alignment(reference, target, maximum_distance=18,
                                                maximum_residual=self.config.maximum_residual, local=True)
            except NeedsReview:
                seed["state"] = "searching" if seed["attempt"] < 2 else "fallback"
                with self.store.transaction() as connection:
                    self.store.guard(connection, job)
                    connection.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s", (Jsonb({"round_seed": seed}), job["id"]))
                if seed["state"] == "searching":
                    raise ContinueJob("Watch-party round seed search checkpointed; expanding the local search")
                LOG.info("watchparty_seed_fallback source_id=%s reason=no_verified_local_visual_anchors", source["id"])
                return False
        seed["state"] = "verified"
        signature = hashlib.sha256(json.dumps([reference, target], sort_keys=True).encode()).hexdigest()
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            connection.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s", (Jsonb({"round_seed": seed}), job["id"]))
            connection.execute("UPDATE pipeline.sources SET checkpoint_time=GREATEST(checkpoint_time,%s),updated_at=now() WHERE id=%s",
                               (max(frame["time"] for frame in target["frames"]), source["id"]))
            self.store.enqueue("twitch_align", f"twitch-round-seed:{source['id']}:r{source['revision']}:{revision}:v1", broadcast=broadcast["id"],
                               source=source["id"], payload={"visual_alignment": {"signature": signature, "mapping": alignment}},
                               priority=-10, connection=connection)
        LOG.info("watchparty_seed_verified source_id=%s frames=%s", source["id"], len(target["frames"]))
        return True

    def event(self, job):
        broadcast = self.store.one(
            "SELECT * FROM pipeline.broadcasts WHERE channel_id=%s AND state IN ('live','ended_waiting_archive') ORDER BY day DESC LIMIT 1",
            (job["payload"]["channel"]["official_youtube_channel_id"],),
        )
        if not broadcast:
            raise WaitingSource("No corresponding official YouTube broadcast yet")
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            apply_event(self.store, connection, job["payload"]["envelope"], job["payload"]["channel"], broadcast)

    def live(self, job):
        source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (job["source_id"],))
        if source["state"] != "live":
            return
        remote = resolve_twitch(source["metadata"]["login"])
        segments = self.media.manifest(remote)
        checkpoint = source["checkpoint_time"]
        for segment in segments[-4:]:
            if not segment["wall_time"]:
                raise WaitingSource("Twitch fingerprints require an HLS wall-clock anchor")
            base = (segment["wall_time"] - source["actual_start"]).total_seconds()
            if base < 0 or base + segment["duration"] <= checkpoint:
                continue
            frames = []
            for offset, frame in self.media.live_frames(remote, segment):
                time = round(base + offset, 3)
                if time <= checkpoint:
                    continue
                if int(time) % 2 == 0:
                    frames.append(
                        {
                            "time": time,
                            "wall_time": (segment["wall_time"] + timedelta(seconds=offset)).isoformat(),
                            **self.media.fingerprint(frame),
                        }
                    )
                checkpoint = time
            self.store.checkpoint(job, source, checkpoint, source["detector_state"], [], frames, timeline="secondary")
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                connection.execute(
                    "UPDATE pipeline.sources SET checkpoint_time=GREATEST(checkpoint_time,%s),updated_at=now() WHERE id=%s",
                    (checkpoint, source["id"]),
                )
            self.store.heartbeat(job, self.config.lease_seconds)

    def align(self, job):
        broadcast, source, _, _ = self.processing.context(job)
        canonical = self.store.one(
            "SELECT * FROM pipeline.sources WHERE broadcast_id=%s AND role='canonical'", (broadcast["id"],)
        )
        timeline = "archive" if broadcast["reconciled_revision"] > 0 else "live"
        reference = self.store.fingerprints(
            canonical, timeline, broadcast["archive_revision"] if timeline == "archive" else None
        )
        target = self.store.fingerprints(source, "secondary")
        target["interval"] = 2
        if timeline == "archive":
            reference["interval"] = 2
        signature = hashlib.sha256(json.dumps([reference, target], sort_keys=True).encode()).hexdigest()
        cached_alignment = job["payload"].get("visual_alignment", {})
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            connection.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s",
                               (Jsonb({"activity": {"phase": "visual_alignment"}}), job["id"]))
        try:
            alignment = cached_alignment["mapping"] if cached_alignment.get("signature") == signature else piecewise_alignment(
                reference, target, maximum_distance=18, maximum_residual=self.config.maximum_residual, local=True
            )
        except NeedsReview as error:
            pending = self.store.one("SELECT id FROM pipeline.jobs WHERE source_id=%s AND kind IN ('twitch_vod','twitch_live') AND state IN ('queued','running','waiting_source','retryable') LIMIT 1", (source["id"],))
            if pending or broadcast["reconciled_revision"] < broadcast["archive_revision"] or broadcast["state"] == "live":
                raise WaitingWork("Twitch alignment is waiting for additional verified fingerprint coverage") from error
            raise
        canonical_revision = broadcast["archive_revision"] if timeline == "archive" else 0
        if source["role"] == "watch_party" and source["metadata"].get("vod_id") and timeline == "archive":
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                connection.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s",
                                   (Jsonb({"visual_alignment": {"signature": signature, "mapping": alignment}}), job["id"]))
            alignment = self.check_rounds(job, broadcast, source, canonical, alignment, canonical_revision)
        self.processing.save_alignment(job, source, canonical, alignment, "secondary", canonical_revision)
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            connection.execute("""UPDATE pipeline.jobs SET state='unsupported',failure_kind='superseded',
                last_error='Superseded by a newer verified alignment',updated_at=now()
                WHERE source_id=%s AND kind='twitch_align' AND generation=%s AND created_at<%s
                AND state IN ('needs_review','unsupported','queued','retryable','waiting_source')""",
                (source["id"], job["generation"], job["created_at"]))
        self.store.enqueue(
            "export", f"chat-export:{source['id']}:{broadcast['archive_revision']}", broadcast=broadcast["id"]
        )

    def check_rounds(self, job, broadcast, source, canonical, alignment, revision):
        begun = time.monotonic()
        indexes = self.store.rows(
            """SELECT DISTINCT ON (expected_match_id) * FROM pipeline.match_indexes
               WHERE broadcast_id=%s AND canonical_source_id=%s AND generation=%s AND archive_revision=%s
               AND state IN ('provisional','final') ORDER BY expected_match_id,version DESC""",
            (broadcast["id"], canonical["id"], broadcast["generation"], revision))
        rounds: list[tuple[str, dict, float]] = []
        for index in indexes:
            shift = index["provenance"].get("clock", {}).get("playback_shift", 0) if index["provenance"].get("method") == "live_presentation_clock" else 0
            rounds.extend((f"{index['expected_match_id']}:{value['map']}:{value['round']}", value, value["start"] - shift)
                          for value in index["rounds"])
        rounds.sort(key=lambda value: value[2])
        if not rounds:
            return alignment
        saved = self.store.one(
            """SELECT mapping FROM pipeline.alignments WHERE source_id=%s AND canonical_source_id=%s
               AND source_revision=%s AND canonical_revision=%s AND kind='secondary'""",
            (source["id"], canonical["id"], source["revision"], revision))
        cached = job["payload"].get("scoreboard_checks", {})
        if cached.get("source_revision") != source["revision"] or cached.get("canonical_revision") != revision:
            cached = {"source_revision": source["revision"], "canonical_revision": revision, "checks": {}}
        checks = {key: check for key, check in (saved or {}).get("mapping", {}).get("roundChecks", {}).items()
                  if check.get("section")}
        checks.update(cached["checks"])
        checks = {key: check for key, value, start in rounds if (check := checks.get(key))
                  and check["canonical_start"] == start and check.get("scores") == value.get("scores")}
        pending = [(key, value, start) for key, value, start in rounds if key not in checks]
        batch = {item[0] for item in pending[:4]}
        remote = resolve_twitch(source["metadata"]["vod_id"], live=False) if pending else {}
        previous = None
        scale = alignment["timelineScale"]
        for key, value, start in rounds:
            if key in checks:
                if checks[key].get("section"):
                    previous = checks[key]["section"]
                continue
            if key not in batch:
                break
            nearest = min(alignment["segments"], key=lambda section: max(section["canonicalStart"] - start, start - section["canonicalEnd"], 0))
            offset = previous["offset"] if previous and 0 < start - previous["canonicalEnd"] < 240 else nearest["offset"]
            predicted = (start - offset) / scale
            section = None
            identity = [key, start, value.get("scores"), source["revision"], revision, predicted, scale]
            search = job["payload"].get("scoreboard_search", {})
            if search.get("identity") != identity:
                search = {"identity": identity, "attempt": 0, "samples": []}
            for attempt, (before, after) in enumerate([(8, 9), (24, 24), (90, 90)]):
                if attempt < search["attempt"]:
                    continue
                with self.store.transaction() as connection:
                    self.store.guard(connection, job)
                    connection.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s",
                                       (Jsonb({"activity": {"phase": "scoreboard" if before == 8 else "searching",
                                                            "completed": len(checks), "total": len(rounds),
                                                            "match_id": key.split(":")[0], "map": value["map"], "round": value["round"]}}), job["id"]))
                lower, upper = max(0, predicted - before), predicted + after
                if remote.get("duration"):
                    upper = min(upper, remote["duration"])
                if upper <= lower:
                    continue
                samples = [observation_from_dict(item) for item in search["samples"]] if attempt == search["attempt"] else []
                resume = max(lower, samples[-1].time + 1) if samples else lower
                window = self.media.watchparty_window(remote, resume, upper)
                try:
                    for sample, _, _ in window:
                        self.processing.check_stop()
                        samples.append(sample)
                        section = scoreboard_section(samples, value, start, scale)
                        if section:
                            break
                        if time.monotonic() - begun >= 45:
                            with self.store.transaction() as connection:
                                self.store.guard(connection, job)
                                connection.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s",
                                                   (Jsonb({"scoreboard_search": {"identity": identity, "attempt": attempt,
                                                                               "samples": [asdict(item) for item in samples]}}), job["id"]))
                            raise ContinueJob("Watch-party scoreboard search checkpointed at its processing time slice")
                finally:
                    window.close()
                if section:
                    break
            checks[key] = {"canonical_start": start, "scores": value.get("scores"), "section": section,
                           "status": "verified" if section else "scoreboard_not_verified"}
            if section:
                previous = section
            cached["checks"] = checks
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                connection.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s",
                                   (Jsonb({"scoreboard_checks": cached, "scoreboard_search": {}, "activity": {"phase": "scoreboard",
                                        "completed": len(checks), "total": len(rounds), "match_id": key.split(":")[0],
                                        "map": value["map"], "round": value["round"]}}), job["id"]))
            self.store.heartbeat(job, self.config.lease_seconds)
            if time.monotonic() - begun >= 45:
                break
        if len(checks) < len(rounds):
            raise ContinueJob(f"Watch-party scoreboard checks: {len(checks)} of {len(rounds)} rounds checked")
        current = self.store.rows("""SELECT DISTINCT ON (expected_match_id) id FROM pipeline.match_indexes
            WHERE broadcast_id=%s AND canonical_source_id=%s AND generation=%s AND archive_revision=%s
            AND state IN ('provisional','final') ORDER BY expected_match_id,version DESC""",
            (broadcast["id"], canonical["id"], broadcast["generation"], revision))
        if {index["id"] for index in current} != {index["id"] for index in indexes}:
            raise WaitingWork("Canonical round evidence changed during watch-party verification; resuming against the latest index")
        return scoreboard_alignment(alignment, checks)

    def chat_gaps(self, job):
        source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (job["source_id"],))
        archive = self.store.one("SELECT * FROM pipeline.chat_archives WHERE source_id=%s", (source["id"],))
        if archive and archive["state"] == "complete":
            return
        vod_id = source["metadata"].get("vod_id")
        if not vod_id and source["metadata"].get("broadcaster_user_id"):
            token, client = os.environ.get("TWITCH_USER_TOKEN"), os.environ.get("TWITCH_CLIENT_ID")
            if token and client:
                query = urlencode(
                    {"user_id": source["metadata"]["broadcaster_user_id"], "type": "archive", "first": 20}
                )
                values = json.loads(
                    fetch_bytes(
                        "https://api.twitch.tv/helix/videos?" + query,
                        {"Authorization": "Bearer " + token, "Client-Id": client},
                    )
                )
                vod_id = next(
                    (video["id"] for video in values["data"] if video.get("stream_id") == source["external_id"]), None
                )
                if vod_id:
                    with self.store.transaction() as connection:
                        self.store.guard(connection, job)
                        connection.execute(
                            "UPDATE pipeline.sources SET metadata=metadata||%s,updated_at=now() WHERE id=%s",
                            (Jsonb({"vod_id": vod_id}), source["id"]),
                        )
        if not vod_id:
            raise WaitingSource(
                "Chat gap recovery is waiting for the Twitch VOD ID; set it with register-source metadata"
            )
        import subprocess
        import tempfile
        from pathlib import Path

        executable = os.environ.get("TWITCH_DOWNLOADER") or (self.config.settings or {}).get("twitch_downloader")
        if not executable:
            raise WaitingSource("TWITCH_DOWNLOADER is required for VOD chat gap recovery")
        if not archive or not source["actual_start"]:
            raise WaitingWork("Chat gap recovery is waiting for Twitch source timing")
        coverage = job["payload"].get("chat_coverage", [])
        width = job["payload"].get("chat_window_seconds", 900)
        window = None
        for gap_start, gap_end in sorted(archive["gaps"]):
            position = gap_start
            for lower, upper in sorted(coverage):
                if upper <= position:
                    continue
                if lower > position:
                    break
                position = max(position, upper)
            if position < gap_end:
                next_covered = min([lower for lower, _ in coverage if lower > position] or [gap_end])
                window = [position, min(position + width, gap_end, next_covered)]
                break
        if window is None:
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                connection.execute("UPDATE pipeline.chat_archives SET state='complete',updated_at=now() WHERE source_id=%s", (source["id"],))
            return
        lower, upper = window
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.json"
            with tempfile.TemporaryFile(mode="w+b") as output:
                result = subprocess.run(
                    [executable, "chatdownload", "--id", vod_id, "--beginning", f"{lower}s", "--ending", f"{upper}s",
                     "--threads", "2", "--collision", "Overwrite", "--temp-path", directory, "-o", str(path)],
                    stdout=output, stderr=output, timeout=300,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode:
                    output.seek(max(0, output.tell() - 1000))
                    raise WaitingSource("Twitch VOD chat recovery failed: " + output.read().decode(errors="replace"))
            if path.stat().st_size > 32 * 1024 * 1024:
                if width <= 30:
                    raise WaitingSource("Twitch chat exceeds the bounded JSON limit even for a 30-second window")
                with self.store.transaction() as connection:
                    self.store.guard(connection, job)
                    connection.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s",
                                       (Jsonb({"chat_window_seconds": max(30, width // 2)}), job["id"]))
                raise ContinueJob("Chat recovery reduced its download window to stay within the memory limit")
            comments = json.loads(path.read_text(encoding="utf-8-sig")).get("comments", [])
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            for message in comments:
                time = float(message["content_offset_seconds"])
                if not math.isfinite(time) or not lower <= time <= upper:
                    continue
                connection.execute(
                    """INSERT INTO pipeline.chat_messages(source_id,message_id,media_time,wall_time,origin,message)
                                   VALUES (%s,%s,%s,%s,'vod',%s) ON CONFLICT DO NOTHING""",
                    (
                        source["id"],
                        message["_id"],
                        time,
                        source["actual_start"] + timedelta(seconds=time),
                        Jsonb(message),
                    ),
                )
            coverage.append(window)
            merged: list[list[float]] = []
            for start, end in sorted(coverage):
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            connection.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s",
                               (Jsonb({"chat_coverage": merged, "activity": {"phase": "chat_recovery", "checkpoint": upper}}), job["id"]))
            latest = connection.execute("SELECT gaps FROM pipeline.chat_archives WHERE source_id=%s FOR UPDATE", (source["id"],)).fetchone()
            complete = all(any(start <= a and b <= end for start, end in merged) for a, b in latest["gaps"])
            connection.execute("UPDATE pipeline.chat_archives SET state=%s,updated_at=now() WHERE source_id=%s",
                               ("complete" if complete else "waiting_vod", source["id"]))
        self.store.enqueue("export", f"chat-recovered:{source['id']}:{upper}", broadcast=source["broadcast_id"])
        if not complete:
            raise ContinueJob("Twitch chat download checkpointed; remaining gaps will recover independently")


def subscribe(session_id, channels, requester=None):
    token, client = os.environ.get("TWITCH_USER_TOKEN"), os.environ.get("TWITCH_CLIENT_ID")
    user = os.environ.get("TWITCH_CHAT_USER_ID")
    if not token or not client or not user:
        raise ValueError("TWITCH_USER_TOKEN, TWITCH_CLIENT_ID, TWITCH_CHAT_USER_ID are required for EventSub")

    def request(body):
        headers = {"Authorization": "Bearer " + token, "Client-Id": client, "Content-Type": "application/json"}
        with urlopen(
            Request(
                "https://api.twitch.tv/helix/eventsub/subscriptions", data=json.dumps(body).encode(), headers=headers
            ),
            timeout=30,
        ) as response:
            return json.load(response)

    for channel in channels:
        for kind in ["stream.online", "stream.offline", "channel.chat.message"]:
            condition = {"broadcaster_user_id": str(channel["broadcaster_user_id"])}
            if kind == "channel.chat.message":
                condition["user_id"] = user
            (requester or request)(
                {
                    "type": kind,
                    "version": "1",
                    "condition": condition,
                    "transport": {"method": "websocket", "session_id": session_id},
                }
            )


def eventsub_loop(store, config, stop):
    from websockets.sync.client import connect

    channels = (config.settings or {}).get("twitch_channels", [])
    if not channels:
        return
    if not all(os.environ.get(name) for name in ["TWITCH_USER_TOKEN", "TWITCH_CLIENT_ID", "TWITCH_CHAT_USER_ID"]):
        LOG.warning("twitch_live_chat_disabled reason=missing_local_user_authorization")
        return
    reconnect_url = None
    failures = 0
    initialized = False
    while not stop.is_set():
        try:
            if not initialized:
                store.execute("UPDATE pipeline.chat_archives a SET disconnected_at=COALESCE(disconnected_at,updated_at) FROM pipeline.sources s WHERE a.source_id=s.id AND s.state='live' AND s.provider='twitch'")
                initialized = True
            channels = resolve_channels(channels)
            url = reconnect_url or "wss://eventsub.wss.twitch.tv/ws"
            preserve = reconnect_url is not None
            with connect(url, open_timeout=30, close_timeout=5) as socket:
                welcome = json.loads(socket.recv(timeout=30))
                session = welcome["payload"]["session"]
                if not preserve:
                    subscribe(session["id"], channels)
                connected = datetime.now(timezone.utc)
                with store.transaction() as connection:
                    for archive in connection.execute(
                        "SELECT a.*,s.actual_start FROM pipeline.chat_archives a JOIN pipeline.sources s ON s.id=a.source_id WHERE s.state='live' AND s.provider='twitch' AND a.disconnected_at IS NOT NULL FOR UPDATE OF a"
                    ).fetchall():
                        gaps = archive["gaps"] + [
                            [
                                max(0, (archive["disconnected_at"] - archive["actual_start"]).total_seconds()),
                                max(0, (connected - archive["actual_start"]).total_seconds()),
                            ]
                        ]
                        connection.execute(
                            "UPDATE pipeline.chat_archives SET gaps=%s,disconnected_at=NULL,updated_at=now() WHERE source_id=%s",
                            (Jsonb(gaps), archive["source_id"]),
                        )
                reconnect_url = None
                while not stop.is_set():
                    value = json.loads(socket.recv(timeout=(session.get("keepalive_timeout_seconds") or 10) + 5))
                    kind = value["metadata"]["message_type"]
                    if kind == "session_reconnect":
                        reconnect_url = value["payload"]["session"]["reconnect_url"]
                        break
                    if kind == "notification":
                        ingest_event(store, value, channels)
                    elif kind == "session_keepalive":
                        failures = 0
                        store.execute(
                            "UPDATE pipeline.chat_archives a SET updated_at=now() FROM pipeline.sources s WHERE a.source_id=s.id AND s.state='live' AND s.provider='twitch'"
                        )
                    elif kind == "revocation":
                        raise NeedsReview(
                            "Twitch revoked subscription: " + json.dumps(value["payload"]["subscription"])
                        )
        except Exception as error:
            LOG.exception("eventsub_disconnected reason=%s", error)
            try:
                store.execute("UPDATE pipeline.chat_archives a SET disconnected_at=COALESCE(disconnected_at,now()) FROM pipeline.sources s WHERE a.source_id=s.id AND s.state='live' AND s.provider='twitch'")
            except Exception:
                LOG.exception("eventsub_gap_record_failed recovery=retry_after_database_returns")
                initialized = False
            from .store import backoff

            reconnect_url = None
            stop.wait(backoff(failures, jitter=True))
            failures = min(failures + 1, 8)


def discover_live_streams(store, channels, requester=fetch_bytes):
    if not channels:
        return
    token, client = os.environ.get("TWITCH_USER_TOKEN"), os.environ.get("TWITCH_CLIENT_ID")
    if not token or not client:
        raise WaitingWork("Twitch live discovery requires TWITCH_USER_TOKEN and TWITCH_CLIENT_ID")
    channels = resolve_channels(channels, requester)
    headers = {"Authorization": "Bearer " + token, "Client-Id": client}
    query = urlencode([("user_id", str(channel["broadcaster_user_id"])) for channel in channels])
    result = json.loads(requester("https://api.twitch.tv/helix/streams?" + query, headers))
    active_users = set()
    for stream in result["data"]:
        active_users.add(stream["user_id"])
        event = {"id": stream["id"], "broadcaster_user_id": stream["user_id"], "started_at": stream["started_at"]}
        ingest_event(
            store,
            {
                "metadata": {
                    "message_id": "poll-online:" + stream["id"],
                    "message_timestamp": datetime.now(timezone.utc).isoformat(),
                },
                "payload": {"subscription": {"type": "stream.online"}, "event": event},
            },
            channels,
        )
    for source in store.rows("SELECT * FROM pipeline.sources WHERE provider='twitch' AND state='live'"):
        user = source["metadata"].get("broadcaster_user_id")
        if user in {str(channel["broadcaster_user_id"]) for channel in channels} and user not in active_users:
            ingest_event(
                store,
                {
                    "metadata": {
                        "message_id": "poll-offline:" + source["external_id"],
                        "message_timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    "payload": {"subscription": {"type": "stream.offline"}, "event": {"broadcaster_user_id": user}},
                },
                channels,
            )
