import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .store import stable_id


LOG = logging.getLogger(__name__)


class ScheduleProvider(Protocol):
    def matches(self) -> list[dict]: ...


def fetch_bytes(url, headers=None):
    with urlopen(Request(url, headers=headers or {}), timeout=30) as response:
        data = response.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024:
            raise ValueError("Source response exceeded the 64 MB limit")
        return data


def riot_events(html):
    decoder = json.JSONDecoder()
    events = {}
    for found in re.finditer(r'\{"__typename":"EventMatch"', html):
        event, _ = decoder.raw_decode(html[found.start() :])
        events[event["id"]] = event
    if not events:
        raise ValueError("Riot schedule response no longer contains EventMatch records")
    return list(events.values())


class RiotSchedule:
    def __init__(self, routes, requester=fetch_bytes, url="https://valorantesports.com/en-US"):
        self.routes = routes
        self.requester = requester
        self.url = url

    def matches(self):
        events = riot_events(self.requester(self.url).decode("utf-8"))
        result = []
        groups = defaultdict(list)
        for event in sorted(events, key=lambda item: (item["startTime"], item["id"])):
            route = next((route for route in self.routes if event["league"]["slug"] == route["league"]), None)
            teams = event.get("matchTeams", [])
            if not route or len(teams) != 2 or any(team.get("code") == "TBD" for team in teams):
                continue
            day = (
                datetime.fromisoformat(event["startTime"].replace("Z", "+00:00"))
                .astimezone(ZoneInfo(route.get("timezone", "UTC")))
                .date()
            )
            groups[(day, route["channel_id"])].append(event["id"])
            result.append(
                {
                    "provider": "riot",
                    "external_id": event["id"],
                    "event": event["tournament"]["name"],
                    "stage": event.get("blockName", ""),
                    "day": day,
                    "team_a": teams[0]["code"],
                    "team_b": teams[1]["code"],
                    "match_order": len(groups[(day, route["channel_id"])]),
                    "best_of": event["match"]["strategy"]["count"],
                    "region": route["region"],
                    "channel_id": route["channel_id"],
                    "completion": {"completed": "completed", "inProgress": "running", "in_progress": "running"}.get(
                        event["state"], "expected"
                    ),
                    "metadata": {
                        "scheduled_start": event["startTime"],
                        "league": event["league"]["slug"],
                        "aliases": {team["code"]: [team["name"], team["code"]] for team in teams},
                    },
                }
            )
        return result


class ManualSchedule:
    def __init__(self, path):
        self.path = Path(path)

    def matches(self):
        values = json.loads(self.path.read_text(encoding="utf-8"))
        for item in values:
            required = {
                "external_id",
                "event",
                "stage",
                "day",
                "team_a",
                "team_b",
                "match_order",
                "best_of",
                "region",
                "channel_id",
            }
            if not required <= item.keys():
                raise ValueError("Manual schedule is missing required fields")
            item.setdefault("provider", "manual")
            item.setdefault("completion", "expected")
            item.setdefault("metadata", {})
            item.setdefault("manual_override", {"reason": "Manual schedule override"})
        return values


def ingest_schedule(store, providers):
    for provider in providers:
        try:
            matches = provider.matches()
        except (OSError, ValueError, KeyError) as error:
            LOG.warning("schedule_unavailable provider=%s reason=%s", type(provider).__name__, error)
            continue
        for match in matches:
            with store.transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM pipeline.expected_matches WHERE provider=%s AND external_id=%s FOR UPDATE",
                    (match["provider"], match["external_id"]),
                ).fetchone()
                if not existing:
                    existing = connection.execute(
                        "SELECT * FROM pipeline.expected_matches WHERE day=%s AND channel_id=%s AND match_order=%s FOR UPDATE",
                        (match["day"], match["channel_id"], match["match_order"]),
                    ).fetchone()
                    if existing and match["provider"] != "manual" and (
                        existing["provider"] == match["provider"] and existing["external_id"] != match["external_id"]
                        or sorted((existing["team_a"], existing["team_b"])) != sorted((match["team_a"], match["team_b"]))
                    ):
                        LOG.warning("schedule_identity_conflict expected_match_id=%s provider=%s external_id=%s day=%s match_order=%s", existing["id"], match["provider"], match["external_id"], match["day"], match["match_order"])
                        continue
                if (
                    existing
                    and any(key != "broadcast_assignment" for key in existing["manual_override"])
                    and match["provider"] != "manual"
                ):
                    if (
                        set(existing["manual_override"]) <= {"broadcast_assignment", "live_identity_repair"}
                        and existing["provider"] == match["provider"]
                        and existing["external_id"] == match["external_id"]
                    ):
                        connection.execute("UPDATE pipeline.expected_matches SET completion=%s,completion_observed_at=CASE WHEN %s='completed' AND completion<>'completed' THEN COALESCE(completion_observed_at,now()) ELSE completion_observed_at END,updated_at=now() WHERE id=%s", (match["completion"], match["completion"], existing["id"]))
                    continue
                match_id = existing["id"] if existing else stable_id("match", match["provider"], match["external_id"])
                connection.execute(
                    """INSERT INTO pipeline.expected_matches(id,event,stage,day,team_a,team_b,match_order,best_of,region,channel_id,provider,external_id,completion,manual_override,metadata)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT(id) DO UPDATE SET event=EXCLUDED.event,stage=EXCLUDED.stage,team_a=EXCLUDED.team_a,team_b=EXCLUDED.team_b,
                                   best_of=EXCLUDED.best_of,completion=EXCLUDED.completion,manual_override=EXCLUDED.manual_override,metadata=EXCLUDED.metadata,updated_at=now()""",
                    (
                        match_id,
                        match["event"],
                        match["stage"],
                        match["day"],
                        match["team_a"],
                        match["team_b"],
                        match["match_order"],
                        match["best_of"],
                        match["region"],
                        match["channel_id"],
                        match["provider"],
                        match["external_id"],
                        match["completion"],
                        Jsonb(
                            {**(existing["manual_override"] if existing else {}), **match.get("manual_override", {})}
                        ),
                        Jsonb(match["metadata"]),
                    ),
                )
                if match["completion"] == "completed" and (existing is None or existing["completion"] != "completed"):
                    connection.execute("UPDATE pipeline.expected_matches SET completion_observed_at=COALESCE(completion_observed_at,now()) WHERE id=%s", (match_id,))
                connection.execute(
                    """UPDATE pipeline.expected_matches e SET broadcast_id=b.id FROM pipeline.broadcasts b
                                   WHERE e.id=%s AND e.day=b.day AND e.channel_id=b.channel_id AND e.broadcast_id IS NULL
                                   AND (SELECT count(*) FROM pipeline.broadcasts other WHERE other.day=e.day AND other.channel_id=e.channel_id)=1""",
                    (match_id,),
                )
