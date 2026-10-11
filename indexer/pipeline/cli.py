import argparse
import json
import math
import os
from datetime import date
from pathlib import Path

from psycopg.types.json import Jsonb
from auto_publish import matchup_key

from .config import Config
from .publishing import export_snapshot
from .schedule import ManualSchedule, ingest_schedule
from .storage import LocalStorage, configured_storage
from .store import Store, identifier, json_bytes, stable_id
from .timeline import compare_indexes


def compare_catalogs(legacy_path, shadow_path):
    def indexes(path):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        result = {}
        for entry in catalog["videos"]:
            if entry.get("provider", "youtube") != "youtube":
                continue
            key = entry.get("expectedMatchId") or entry["sourceId"] + ":" + entry["title"].upper()
            index = json.loads((path.parent / entry["index"].lstrip("/")).read_text(encoding="utf-8"))
            result[key] = index
        return result

    legacy, shadow = indexes(Path(legacy_path)), indexes(Path(shadow_path))
    left_catalog = json.loads(Path(legacy_path).read_text(encoding="utf-8"))
    right_catalog = json.loads(Path(shadow_path).read_text(encoding="utf-8"))
    for right in right_catalog["videos"]:
        new_key = right.get("expectedMatchId") or right["sourceId"] + ":" + right["title"].upper()
        if new_key in legacy:
            continue
        candidates = [
            left
            for left in left_catalog["videos"]
            if left.get("provider", "youtube") == "youtube"
            and left.get("sourceId") == right.get("sourceId")
            and left["title"].upper() == right["title"].upper()
        ]
        if len(candidates) == 1:
            left = candidates[0]
            old_key = left.get("expectedMatchId") or left["sourceId"] + ":" + left["title"].upper()
            legacy[new_key] = legacy.pop(old_key)
    return compare_indexes(legacy, shadow)


def release_to_site(release, site):
    def identity(entry):
        return entry.get("catalogId") if str(entry.get("catalogId", "")).startswith("twitch:") else entry.get("expectedMatchId")

    def version(entry):
        return tuple(entry.get(name, 0) for name in ("pipelineGeneration", "pipelineVersion", "pipelineSourceRevision", "pipelineAlignmentVersion"))

    release, site = Path(release), Path(site)
    incoming = json.loads((release / "catalog.json").read_text(encoding="utf-8"))
    catalog = json.loads((site / "catalog.json").read_text(encoding="utf-8"))
    incoming_ids = {identity(entry) for entry in incoming["videos"]}
    incoming_titles = {(entry.get("provider", "youtube"), entry["sourceId"], matchup_key(entry["title"]) or entry["title"].upper()) for entry in incoming["videos"]}
    withheld_ids = set()
    withheld_records = {identity(value): value for value in catalog.get("withheld", [])}
    withheld = list(incoming.get("withheld", []))
    for parent in incoming.get("withheld", []):
        if str(parent.get("catalogId", "")).startswith("twitch:"):
            continue
        for previous in catalog["videos"] + catalog.get("withheld", []):
            if previous.get("provider") == "twitch" and previous.get("canonicalPipeline") and previous.get("expectedMatchId") == parent["expectedMatchId"]:
                withheld.append({**previous, **parent, "catalogId": previous["catalogId"]})
    for entry in withheld:
        match_id = identity(entry)
        if match_id in incoming_ids:
            raise ValueError("A release cannot both publish and withhold the same match")
        previous = next((value for value in catalog["videos"] + catalog.get("withheld", []) if identity(value) == match_id), None)
        owned_variant = entry.get("provider") == "twitch" and any(value.get("provider") == "youtube" and value["expectedMatchId"] == entry["expectedMatchId"] for value in incoming["videos"])
        if (previous and previous.get("canonicalPipeline") and version(previous) <= version(entry)) or (owned_variant and (not previous or not previous.get("canonicalPipeline"))):
            withheld_ids.add(match_id)
            withheld_records[match_id] = {**(previous or {}), **entry, "canonicalPipeline": True}
    artifacts = {}
    release_storage = LocalStorage(release)
    for entry in incoming["videos"]:
        previous = next(
            (value for value in catalog["videos"] + catalog.get("withheld", []) if identity(value) == identity(entry)), None
        )
        if previous and version(previous) > version(entry):
            raise ValueError("Release snapshot is older than the installed index")
        path = entry["index"].lstrip("/")
        if not path.startswith("indexes/"):
            raise ValueError("Release index must be stored under indexes/")
        data = release_storage.get(path)
        index = json.loads(data)
        if (
            entry.get("provider") not in {"youtube", "twitch"}
            or entry.get("pipelineState") not in {"provisional", "final"}
            or index.get("schemaVersion") != 2
            or index.get("provider") != entry.get("provider")
            or index.get("sourceId") != entry["sourceId"]
            or index.get("pipelineState") != entry["pipelineState"]
            or index.get("pipelineVersion") != entry["pipelineVersion"]
            or not index.get("rounds")
        ):
            raise ValueError("Release index does not match its canonical catalog entry")
        if entry["provider"] == "twitch":
            parent = next((value for value in incoming["videos"] if value.get("provider") == "youtube" and value["expectedMatchId"] == entry["expectedMatchId"]), None)
            parent_index = json.loads(release_storage.get(parent["index"].lstrip("/"))) if parent else {}
            if not parent or not index.get("derivedFromCanonical") or index.get("canonical", {}).get("sourceId") != parent["sourceId"] or index["canonical"].get("rounds") != parent_index.get("rounds") or version(entry)[:2] != version(parent)[:2] or index.get("sourceRevision") != entry.get("pipelineSourceRevision") or index.get("alignmentVersion") != entry.get("pipelineAlignmentVersion"):
                raise ValueError("Watch-party release is not derived from its current canonical index")
            alignment = index.get("alignment", {})
            scale = alignment.get("timelineScale", 0)
            if not isinstance(scale, (int, float)) or not math.isfinite(scale) or not .95 <= scale <= 1.05 or alignment.get("source") != "youtube:" + parent["sourceId"] or not alignment.get("strictCoverage") or len(index["rounds"]) != len(parent_index["rounds"]) or index.get("pipelineState") != parent.get("pipelineState"):
                raise ValueError("Watch-party release has invalid canonical alignment")
            for target, canonical in zip(index["rounds"], parent_index["rounds"]):
                sections = [section for section in alignment.get("segments", []) if section["targetStart"] <= target["start"] - 5 and section["targetEnd"] >= target["start"] + 2]
                if (target["map"], target["round"]) != (canonical["map"], canonical["round"]) or len(sections) != 1 or abs(scale * target["start"] + sections[0]["offset"] - canonical["start"]) > .002:
                    raise ValueError("Watch-party rounds do not match verified canonical timestamps")
        artifacts[path] = data
        if entry.get("chat"):
            chat_path = entry["chat"].lstrip("/")
            if not chat_path.startswith("chats/"):
                raise ValueError("Release chat must be stored under chats/")
            artifacts[chat_path] = release_storage.get(chat_path)
    catalog["videos"] = [
        entry
        for entry in catalog["videos"]
        if not (
            entry.get("provider") in {"youtube", "twitch"}
            and (
                identity(entry) in incoming_ids | withheld_ids
                or (entry.get("provider", "youtube"), entry.get("sourceId"), matchup_key(entry.get("title", "")) or entry.get("title", "").upper()) in incoming_titles
            )
        )
    ]
    storage = LocalStorage(site)
    for path, data in artifacts.items():
        storage.put(path, data)
    for entry in incoming["videos"]:
        catalog["videos"].append({**entry, "canonicalPipeline": True})
    catalog["withheld"] = [value for key, value in withheld_records.items() if key not in incoming_ids]
    catalog["videos"].sort(key=lambda entry: entry.get("playedAt", ""), reverse=True)
    storage.put("catalog.json", json_bytes(catalog))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("SPOILLESS_PIPELINE_CONFIG"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    metrics = commands.add_parser("metrics")
    metrics.add_argument("--broadcast")
    commands.add_parser("discover")
    inspect = commands.add_parser("inspect-broadcast")
    inspect.add_argument("id")
    inspect_job = commands.add_parser("inspect-job")
    inspect_job.add_argument("id")
    retry = commands.add_parser("retry-job")
    retry.add_argument("id")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("broadcast")
    reconcile.add_argument("--new-revision", action="store_true")
    recover = commands.add_parser("reprocess-range")
    recover.add_argument("broadcast")
    recover.add_argument("start", type=float)
    recover.add_argument("end", type=float)
    schedule = commands.add_parser("ingest-schedule")
    schedule.add_argument("file")
    assign = commands.add_parser("assign-match")
    assign.add_argument("match")
    assign.add_argument("broadcast")
    assign.add_argument("--reason", required=True)
    source = commands.add_parser("register-source")
    source.add_argument("broadcast")
    source.add_argument("provider", choices=["youtube", "twitch"])
    source.add_argument("external_id")
    source.add_argument("role", choices=["full_match", "official_twitch", "watch_party"])
    source.add_argument("--metadata", default="{}")
    review = commands.add_parser("review-segment")
    review.add_argument("id")
    review.add_argument("--match")
    review.add_argument("--reason")
    export = commands.add_parser("export")
    export.add_argument("--release", action="store_true")
    export.add_argument("--broadcast")
    rollout = commands.add_parser("prepare-rollout")
    rollout.add_argument("youtube_id")
    rollout.add_argument("--day", type=date.fromisoformat, required=True)
    rollout.add_argument("--output", required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("legacy")
    compare.add_argument("shadow")
    compare.add_argument("--output")
    install = commands.add_parser("install-release")
    install.add_argument("release")
    install.add_argument("site")
    args = parser.parse_args()
    if args.command == "compare":
        report = compare_catalogs(args.legacy, args.shadow)
        if args.output:
            Path(args.output).write_bytes(json_bytes(report))
        print(json.dumps(report, indent=2))
        return
    if args.command == "install-release":
        release_to_site(args.release, args.site)
        return
    config = Config.load(args.config)
    store = Store(config.database_url)
    if args.command == "prepare-rollout":
        broadcast = store.one("SELECT * FROM pipeline.broadcasts WHERE youtube_id=%s", (args.youtube_id,))
        settings = dict(config.settings or {})
        channels = {item["channel_id"] for item in settings.get("youtube_channels", [])}
        if not broadcast or broadcast["day"] != args.day or broadcast["channel_id"] not in channels or broadcast["state"] == "unavailable":
            raise ValueError("Rollout requires a discovered official broadcast on the explicitly approved day/channel")
        matches = store.rows("SELECT id,team_a,team_b,stage FROM pipeline.expected_matches WHERE broadcast_id=%s ORDER BY match_order", (broadcast["id"],))
        if not matches:
            raise ValueError("Associate the expected matches before preparing the rollout")
        output = Path(args.output).resolve()
        if args.config and output == Path(args.config).resolve():
            raise ValueError("Use a separate rollout configuration; the running worker configuration is preserved")
        settings["approved_broadcasts"] = [args.youtube_id]
        LocalStorage(output.parent).put(output.name, json_bytes(settings))
        print(json.dumps({"config": str(output), "youtube_id": args.youtube_id, "day": str(args.day), "matches": matches, "mode_environment": "SPOILLESS_PIPELINE_MODE=publish"}, default=str, indent=2))
    elif args.command == "migrate":
        store.migrate()
    elif args.command == "metrics":
        print(json.dumps(store.performance(args.broadcast), default=str, indent=2))
    elif args.command == "discover":
        from .coordinator import Coordinator

        Coordinator(store, config).discover()
    elif args.command == "inspect-broadcast":
        print(json.dumps(store.status(args.id), default=str, indent=2))
    elif args.command == "assign-match":
        with store.transaction() as connection:
            broadcast = connection.execute(
                "SELECT * FROM pipeline.broadcasts WHERE id=%s FOR UPDATE", (args.broadcast,)
            ).fetchone()
            match = connection.execute(
                "SELECT * FROM pipeline.expected_matches WHERE id=%s FOR UPDATE", (args.match,)
            ).fetchone()
            if (
                not broadcast
                or not match
                or (broadcast["day"], broadcast["channel_id"]) != (match["day"], match["channel_id"])
            ):
                raise ValueError("Expected match and broadcast must share the scheduled day and official channel")
            if (
                match["broadcast_id"] != broadcast["id"]
                and connection.execute(
                    "SELECT id FROM pipeline.match_indexes WHERE expected_match_id=%s LIMIT 1", (match["id"],)
                ).fetchone()
            ):
                raise ValueError("An indexed match cannot be moved to another broadcast")
            connection.execute(
                "UPDATE pipeline.expected_matches SET broadcast_id=%s,manual_override=manual_override||%s,updated_at=now() WHERE id=%s",
                (broadcast["id"], Jsonb({"broadcast_assignment": args.reason}), match["id"]),
            )
            store.enqueue("segment", f"segment:{broadcast['id']}", broadcast=broadcast["id"], connection=connection)
    elif args.command == "inspect-job":
        print(
            json.dumps(
                {
                    "job": store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (args.id,)),
                    "attempts": store.rows(
                        "SELECT * FROM pipeline.attempts WHERE job_id=%s ORDER BY number", (args.id,)
                    ),
                },
                default=str,
                indent=2,
            )
        )
    elif args.command == "retry-job":
        store.retry_job(args.id)
    elif args.command in {"reconcile", "reprocess-range"}:
        with store.transaction() as connection:
            broadcast = connection.execute(
                "SELECT * FROM pipeline.broadcasts WHERE id=%s FOR UPDATE", (args.broadcast,)
            ).fetchone()
            source = connection.execute(
                "SELECT * FROM pipeline.sources WHERE broadcast_id=%s AND role='canonical'", (args.broadcast,)
            ).fetchone()
            if not broadcast or not source:
                raise ValueError("Broadcast/canonical source not found")
            if args.command == "reconcile":
                if args.new_revision:
                    revision = broadcast["archive_revision"] + 1
                    connection.execute(
                        "UPDATE pipeline.broadcasts SET generation=generation+1,archive_revision=%s,reconciled_revision=0,updated_at=now() WHERE id=%s",
                        (revision, args.broadcast),
                    )
                else:
                    revision = broadcast["archive_revision"]
                store.enqueue(
                    "reconcile",
                    f"reconcile:{args.broadcast}:{revision}",
                    broadcast=args.broadcast,
                    source=source["id"],
                    connection=connection,
                )
            else:
                if not 0 <= args.start < args.end:
                    raise ValueError("Invalid recovery range")
                store.enqueue(
                    "recover",
                    f"manual-range:{identifier()}",
                    broadcast=args.broadcast,
                    source=source["id"],
                    payload={"start": args.start, "end": args.end, "stage": 2, "timeline": "archive"},
                    connection=connection,
                )
    elif args.command == "ingest-schedule":
        ingest_schedule(store, [ManualSchedule(args.file)])
    elif args.command == "register-source":
        metadata = json.loads(args.metadata)
        if args.provider == "twitch":
            if not args.external_id.isdigit() or args.role not in {"official_twitch", "watch_party"}:
                raise ValueError("Twitch registration requires a numeric VOD ID and official_twitch or watch_party role")
            metadata["vod_id"] = args.external_id
        with store.transaction() as connection:
            source = connection.execute(
                """INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,metadata)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(broadcast_id,provider,external_id) DO UPDATE SET metadata=sources.metadata||EXCLUDED.metadata,updated_at=now() RETURNING *""",
                (
                    stable_id("source", args.broadcast, args.provider, args.external_id),
                    args.broadcast,
                    args.provider,
                    args.external_id,
                    args.role,
                    Jsonb(metadata),
                ),
            ).fetchone()
            if source["role"] != args.role:
                raise ValueError("An existing source cannot be registered with a different role")
            if args.provider == "twitch":
                existing = connection.execute("SELECT id FROM pipeline.jobs WHERE dedupe_key=%s", (f"twitch_vod:{source['id']}",)).fetchone()
                if not existing:
                    store.enqueue("twitch_vod", f"twitch_vod:{source['id']}", broadcast=args.broadcast, source=source["id"], priority=-30, connection=connection)
    elif args.command == "review-segment":
        segment = store.one("SELECT * FROM pipeline.segments WHERE id=%s", (args.id,))
        if args.match:
            if not args.reason:
                raise ValueError("A review reason is required")
            with store.transaction() as connection:
                segment = connection.execute(
                    "SELECT * FROM pipeline.segments WHERE id=%s FOR UPDATE", (args.id,)
                ).fetchone()
                match = connection.execute(
                    "SELECT * FROM pipeline.expected_matches WHERE id=%s AND broadcast_id=%s",
                    (args.match, segment["broadcast_id"]),
                ).fetchone()
                if not match:
                    raise ValueError("Match must belong to the segment broadcast")
                findings = [
                    finding
                    for finding in segment["findings"]
                    if finding["code"] not in {"unassigned_match", "schedule_order_disagreement"}
                ]
                connection.execute(
                    "UPDATE pipeline.segments SET expected_match_id=%s,findings=%s,evidence=evidence||%s,revision=revision+1,updated_at=now() WHERE id=%s",
                    (args.match, Jsonb(findings), Jsonb({"manual_assignment": args.reason}), args.id),
                )
                store.enqueue(
                    "validate",
                    f"manual-review:{identifier()}",
                    broadcast=segment["broadcast_id"],
                    match=args.match,
                    payload={"segment_id": args.id},
                    connection=connection,
                )
        print(json.dumps(store.one("SELECT * FROM pipeline.segments WHERE id=%s", (args.id,)), default=str, indent=2))
    elif args.command == "export":
        if args.release and config.shadow:
            raise ValueError("Release export requires reviewed publish mode")
        approved = config.approved_ids(store) if args.release else None
        if args.broadcast:
            if args.release and args.broadcast not in (approved or []):
                raise ValueError("Broadcast has not been approved for release")
            approved = [args.broadcast]
        print(
            json.dumps(
                export_snapshot(store, configured_storage(config), shadow=not args.release, approved_broadcasts=approved), default=str, indent=2
            )
        )


if __name__ == "__main__":
    main()
