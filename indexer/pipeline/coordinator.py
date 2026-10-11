import logging
from datetime import datetime, timezone

from psycopg.types.json import Jsonb
from storyboard_align import ALIGNER_VERSION

from .schedule import ManualSchedule, RiotSchedule, ScheduleProvider, ingest_schedule
from .store import stable_id
from .youtube import YouTube
from .errors import WaitingWork


LOG = logging.getLogger(__name__)


class Coordinator:
    def __init__(self, store, config, youtube=None):
        self.store = store
        self.config = config
        self.youtube = youtube or YouTube()

    def discover(self):
        settings = self.config.settings or {}
        providers: list[ScheduleProvider] = (
            [RiotSchedule(settings.get("schedule_routes", []))] if settings.get("schedule_routes") else []
        )
        if settings.get("manual_schedule"):
            providers.append(ManualSchedule(settings["manual_schedule"]))
        ingest_schedule(self.store, providers)
        if settings.get("twitch_channels"):
            from .twitch import discover_live_streams

            try:
                discover_live_streams(self.store, settings["twitch_channels"])
            except WaitingWork as error:
                LOG.info("twitch_discovery_waiting reason=%s", error)
            except Exception:
                LOG.exception("twitch_discovery_failed")
            for channel in settings["twitch_channels"]:
                bucket = int(datetime.now(timezone.utc).timestamp() // 1800)
                self.store.enqueue("discover_twitch_vods", f"discover-twitch-vods:{channel['login']}:{bucket}",
                                   payload={"channel": channel}, priority=-50)
        if not self.config.shadow and settings.get("deployment"):
            self.store.enqueue("deploy", "deploy:site", priority=50)
        routes: dict[str, list[dict]] = {}
        for setting, kind in [("youtube_channels", "associate"), ("full_match_channels", "associate_upload")]:
            for channel in settings.get(setting, []):
                routes.setdefault(channel["channel_id"], []).append({"kind": kind, "channel": channel})
        for channel_id, channel_routes in routes.items():
            self.store.enqueue(
                "discover_youtube", f"discover-youtube:{channel_id}", payload={"routes": channel_routes}
            )
        for broadcast in self.store.rows(
            "SELECT * FROM pipeline.broadcasts WHERE state NOT IN ('unavailable') ORDER BY day DESC LIMIT 100"
        ):
            age = (datetime.now(timezone.utc).date() - broadcast["day"]).days
            if (
                age > settings.get("lookback_days", 14)
                and broadcast["reconciled_revision"] == broadcast["archive_revision"]
            ):
                continue
            settled = age > 0 and broadcast["state"] == "archive_ready" and broadcast["reconciled_revision"] == broadcast["archive_revision"]
            if not settled or (datetime.now(timezone.utc) - broadcast["updated_at"]).total_seconds() >= 21600:
                self.store.enqueue("refresh", f"refresh:{broadcast['id']}", broadcast=broadcast["id"])
            self.store.enqueue("segment", f"segment:{broadcast['id']}", broadcast=broadcast["id"], delay=1)
        for source in self.store.rows(
            "SELECT s.*,b.reconciled_revision FROM pipeline.sources s JOIN pipeline.broadcasts b ON b.id=s.broadcast_id WHERE s.provider='twitch' AND s.checkpoint_time>=60"
        ):
            bucket = int(source["checkpoint_time"] // 300)
            key = f"twitch-local-alignment:{source['id']}:r{source['reconciled_revision']}:{bucket}"
            if source["role"] == "watch_party" and source["metadata"].get("vod_id") and source["reconciled_revision"] > 0:
                indexes = self.store.rows("""SELECT i.expected_match_id,max(i.version) AS version FROM pipeline.match_indexes i
                    JOIN pipeline.broadcasts b ON b.id=i.broadcast_id WHERE i.broadcast_id=%s AND i.generation=b.generation
                    AND i.archive_revision=%s AND i.state IN ('provisional','final') GROUP BY i.expected_match_id ORDER BY i.expected_match_id""",
                    (source["broadcast_id"], source["reconciled_revision"]))
                key += ":scoreboard-v2:" + str(stable_id(indexes))
            with self.store.transaction() as connection:
                if not connection.execute("SELECT id FROM pipeline.jobs WHERE dedupe_key=%s", (key,)).fetchone():
                    prior = connection.execute("""SELECT payload FROM pipeline.jobs WHERE source_id=%s AND kind='twitch_align'
                        AND generation=(SELECT generation FROM pipeline.broadcasts WHERE id=%s)
                        AND last_error='Twitch scoreboard window exceeded the memory limit' ORDER BY created_at DESC LIMIT 1""",
                        (source["id"], source["broadcast_id"])).fetchone()
                    self.store.enqueue(
                        "twitch_align",
                        key,
                        broadcast=source["broadcast_id"],
                        source=source["id"],
                        payload=prior["payload"] if prior else None,
                        priority=-40,
                        connection=connection,
                    )

    def discover_youtube(self, job):
        from .errors import WaitingSource

        failures = []
        for route in job["payload"]["routes"]:
            channel, kind = route["channel"], route["kind"]
            try:
                entries = self.youtube.discover(channel, uploads=kind == "associate_upload")
            except WaitingSource as error:
                failures.append(str(error))
                continue
            for entry in entries:
                if kind == "associate":
                    known = self.store.one("SELECT id FROM pipeline.broadcasts WHERE youtube_id=%s", (entry["id"],))
                else:
                    known = self.store.one(
                        "SELECT id FROM pipeline.sources WHERE provider='youtube' AND external_id=%s AND role='full_match' LIMIT 1",
                        (entry["id"],),
                    )
                if not known:
                    self.store.enqueue(
                        kind, ("associate:" if kind == "associate" else "associate-upload:") + entry["id"],
                        payload={"entry": entry, "channel": channel},
                    )
        if failures:
            raise WaitingSource("; ".join(failures))

    def associate(self, job):
        entry, channel = job["payload"]["entry"], job["payload"]["channel"]
        info = self.youtube.metadata(entry["id"])
        if info["state"] == "discovered" and not info["metadata"].get("liveStreamingDetails"):
            from .errors import Unsupported

            raise Unsupported("An ordinary video upload cannot be registered as an official broadcast")
        actual = info["actual_start"]
        scheduled = info["metadata"].get("liveStreamingDetails", {}).get("scheduledStartTime")
        from .youtube import timestamp
        from zoneinfo import ZoneInfo

        clock = actual or timestamp(scheduled) or (
            timestamp(info["metadata"].get("release_timestamp")) if info["state"] == "scheduled" else None
        ) or timestamp(entry.get("published"))
        if not clock:
            from .errors import WaitingSource

            raise WaitingSource("Broadcast day is unavailable")
        day = clock.astimezone(ZoneInfo(channel.get("timezone", "UTC"))).date()
        existing = self.store.one("SELECT id FROM pipeline.broadcasts WHERE youtube_id=%s", (entry["id"],))
        broadcast_id = existing["id"] if existing else stable_id("broadcast", entry["id"])
        existing_source = self.store.one(
            "SELECT id FROM pipeline.sources WHERE broadcast_id=%s AND role='canonical'", (broadcast_id,)
        )
        source_id = (
            existing_source["id"] if existing_source else stable_id("source", broadcast_id, "youtube", entry["id"])
        )
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            connection.execute(
                """INSERT INTO pipeline.broadcasts(id,event,day,region,channel_id,youtube_id,state,actual_start,actual_end,metadata)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(youtube_id) DO NOTHING""",
                (
                    broadcast_id,
                    channel.get("event", entry.get("title", "Official broadcast")),
                    day,
                    channel["region"],
                    channel["channel_id"],
                    entry["id"],
                    info["state"],
                    info["actual_start"],
                    info["actual_end"],
                    Jsonb(info["metadata"]),
                ),
            )
            connection.execute(
                """INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,actual_start)
                               VALUES (%s,%s,'youtube',%s,'canonical',%s) ON CONFLICT DO NOTHING""",
                (source_id, broadcast_id, entry["id"], actual),
            )
            connection.execute(
                """UPDATE pipeline.expected_matches SET broadcast_id=%s WHERE day=%s AND channel_id=%s AND broadcast_id IS NULL
                               AND (SELECT count(*) FROM pipeline.broadcasts WHERE day=%s AND channel_id=%s)=1""",
                (broadcast_id, day, channel["channel_id"], day, channel["channel_id"]),
            )
            self.store.enqueue(
                "refresh", f"refresh:{broadcast_id}", broadcast=broadcast_id, source=source_id, connection=connection
            )
        LOG.info(
            "broadcast_discovered broadcast_id=%s source_id=%s youtube_id=%s day=%s",
            broadcast_id,
            source_id,
            entry["id"],
            day,
        )

    def associate_upload(self, job):
        from .errors import NeedsReview, WaitingSource
        from .media import recognized_teams

        entry, channel = job["payload"]["entry"], job["payload"]["channel"]
        matches = self.store.rows(
            "SELECT * FROM pipeline.expected_matches WHERE channel_id=%s AND broadcast_id IS NOT NULL AND day>=CURRENT_DATE-14",
            (channel["official_channel_id"],),
        )
        selected = []
        for match in matches:
            aliases = match["metadata"].get("aliases", {})
            aliases = {team: aliases.get(team, [team]) for team in [match["team_a"], match["team_b"]]}
            if recognized_teams([(entry["title"], 1)], aliases):
                selected.append(match)
        if not selected:
            raise WaitingSource("Full-match identity is waiting for expected-match metadata")
        if len(selected) != 1:
            raise NeedsReview("Full-match title matches more than one expected match; register-source manually")
        match = selected[0]
        source_id = stable_id("source", match["broadcast_id"], "youtube", entry["id"])
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            connection.execute(
                """INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,metadata)
                               VALUES (%s,%s,'youtube',%s,'full_match',%s) ON CONFLICT DO NOTHING""",
                (
                    source_id,
                    match["broadcast_id"],
                    entry["id"],
                    Jsonb({"expected_match_id": str(match["id"]), "title": entry["title"]}),
                ),
            )
            self.store.enqueue(
                "validate_upload",
                f"validate-upload:{source_id}",
                broadcast=match["broadcast_id"],
                source=source_id,
                match=match["id"],
                priority=-20,
                connection=connection,
            )

    def refresh(self, job):
        broadcast = self.store.one("SELECT * FROM pipeline.broadcasts WHERE id=%s", (job["broadcast_id"],))
        if broadcast["metadata"].get("manual_exclusion"):
            return
        source = self.store.one(
            "SELECT * FROM pipeline.sources WHERE broadcast_id=%s AND role='canonical'", (broadcast["id"],)
        )
        info = self.youtube.metadata(broadcast["youtube_id"])
        state = info["state"]
        revision = broadcast["archive_revision"]
        if state == "ended_waiting_archive":
            if broadcast["state"] == "archive_ready":
                state = "archive_ready"
            else:
                self.store.enqueue(
                    "probe_archive", f"probe_archive:{broadcast['id']}", broadcast=broadcast["id"], source=source["id"]
                )
        if broadcast["state"] == "archive_ready" and state == "discovered":
            state = "archive_ready"
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            if state == "scheduled" and not broadcast["actual_start"] and source["checkpoint_time"] < 0:
                from .youtube import timestamp
                from zoneinfo import ZoneInfo

                scheduled = info["metadata"].get("liveStreamingDetails", {}).get("scheduledStartTime")
                clock = timestamp(scheduled) or timestamp(info["metadata"].get("release_timestamp"))
                channel: dict = next((item for item in (self.config.settings or {}).get("youtube_channels", []) if item["channel_id"] == broadcast["channel_id"]), {})
                if clock and not connection.execute("SELECT id FROM pipeline.match_indexes WHERE broadcast_id=%s LIMIT 1", (broadcast["id"],)).fetchone() and not connection.execute("SELECT id FROM pipeline.round_candidates WHERE broadcast_id=%s LIMIT 1", (broadcast["id"],)).fetchone():
                    day = clock.astimezone(ZoneInfo(channel.get("timezone", "UTC"))).date()
                    if day != broadcast["day"]:
                        connection.execute("UPDATE pipeline.broadcasts SET day=%s WHERE id=%s", (day, broadcast["id"]))
                        connection.execute("UPDATE pipeline.expected_matches SET broadcast_id=NULL,updated_at=now() WHERE broadcast_id=%s AND day<>%s AND manual_override='{}'::jsonb", (broadcast["id"], day))
                        connection.execute("UPDATE pipeline.expected_matches SET broadcast_id=%s,updated_at=now() WHERE day=%s AND channel_id=%s AND broadcast_id IS NULL AND (SELECT count(*) FROM pipeline.broadcasts WHERE day=%s AND channel_id=%s)=1", (broadcast["id"], day, broadcast["channel_id"], day, broadcast["channel_id"]))
                        LOG.info("broadcast_schedule_corrected broadcast_id=%s previous_day=%s day=%s", broadcast["id"], broadcast["day"], day)
            connection.execute(
                """UPDATE pipeline.broadcasts SET state=CASE WHEN state='archive_ready' AND %s<>'live' THEN state ELSE %s END,
                               actual_start=COALESCE(actual_start,%s),actual_end=COALESCE(actual_end,%s),metadata=%s,updated_at=now() WHERE id=%s""",
                (state, state, info["actual_start"], info["actual_end"], Jsonb(info["metadata"]), broadcast["id"]),
            )
            if state == "live":
                self.store.enqueue(
                    "live",
                    f"live:{source['id']}",
                    broadcast=broadcast["id"],
                    source=source["id"],
                    priority=100,
                    connection=connection,
                )
            elif state == "archive_ready" and broadcast["reconciled_revision"] < revision:
                self.store.enqueue(
                    "reconcile",
                    f"reconcile:{broadcast['id']}:{revision}",
                    broadcast=broadcast["id"],
                    source=source["id"],
                    payload={"aligner_version": ALIGNER_VERSION},
                    priority=30,
                    connection=connection,
                )
        if state != broadcast["state"]:
            LOG.info(
                "broadcast_transition broadcast_id=%s previous=%s state=%s", broadcast["id"], broadcast["state"], state
            )
