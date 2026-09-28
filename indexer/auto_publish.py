import argparse
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import server
import chat_archive
from detector import DETECTOR_VERSION
from storyboard_align import ALIGNER_VERSION, align_storyboards, extract_storyboard, load_storyboard, save_storyboard, translate_index


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
CONFIG_PATH = Path(__file__).with_name("auto_channels.json")
STATE_PATH = Path(__file__).with_name("auto_state.json")
STORYBOARDS = Path(__file__).with_name("storyboards")
DIAGNOSTICS = Path(__file__).with_name("diagnostics")
PIPELINE_VERSION = DETECTOR_VERSION + "+" + ALIGNER_VERSION


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def preserve_diagnostics(provider, identifier, work):
    source = work / "diagnostics"
    if not source.is_dir():
        return
    DIAGNOSTICS.mkdir(exist_ok=True)
    destination = DIAGNOSTICS / f"{provider}-{identifier}"
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(source, destination)


def clean_text(value):
    return re.sub(r"\s+", " ", str(value).replace("—", "-").replace("–", "-")).strip()


def source_key(provider, identifier):
    return f"{provider}:{identifier}"


def retry_class(message):
    permanent = (
        "No reliable round starts were found",
        "The first detected round is not round 1",
        "The index does not begin at map 1 round 1",
        "The detected round sequence contains a gap",
        "At least one round is below the automatic confidence threshold",
        "The aligned match does not begin at map 1 round 1",
        "The translated round sequence contains a gap",
        "More than one official broadcast matches this YouTube full match",
    )
    if any(reason.lower() in str(message).lower() for reason in permanent):
        return "pipeline-update"
    if re.search(r"Only \d+ rounds were detected", str(message), re.IGNORECASE):
        return "pipeline-update"
    return "cooldown"


def should_attempt(key, published, state, retry_hours, now):
    if key in published:
        return False
    previous = state.get(key)
    if not previous:
        return True
    if previous.get("status") == "published":
        return True
    previous_version = previous.get("pipelineVersion")
    if previous_version and previous_version != PIPELINE_VERSION:
        return True
    if not previous_version and previous.get("detectorVersion") != DETECTOR_VERSION:
        return True
    if previous.get("retryClass", retry_class(previous.get("message", ""))) == "pipeline-update":
        return False
    checked = datetime.fromisoformat(previous["checkedAt"])
    return (now - checked).total_seconds() >= retry_hours * 3600


def is_candidate(channel, entry, require_duration=True):
    title = clean_text(entry.get("title", ""))
    identifier = str(entry.get("id", ""))
    duration = entry.get("duration")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
        return False
    if require_duration and (not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < channel["minimumDuration"]):
        return False
    if duration is not None and (not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < channel["minimumDuration"]):
        return False
    if entry.get("live_status") in {"is_live", "is_upcoming"}:
        return False
    if not re.search(channel["includeTitle"], title, re.IGNORECASE):
        return False
    return not re.search(channel["excludeTitle"], title, re.IGNORECASE)


def twitch_duration(value):
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", str(value))
    if not match or not any(match.groups()):
        return 0
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def is_twitch_candidate(channel, entry, live_stream_ids):
    identifier = str(entry.get("id", ""))
    title = clean_text(entry.get("title", ""))
    if not re.fullmatch(r"[0-9]{6,20}", identifier) or entry.get("type") != "archive":
        return False
    if str(entry.get("stream_id", "")) in live_stream_ids:
        return False
    duration = twitch_duration(entry.get("duration"))
    if duration < channel["minimumDuration"]:
        return False
    if channel.get("maximumDuration") and duration > channel["maximumDuration"]:
        return False
    if channel.get("requireMatchup") and not re.search(r"\b[A-Z0-9][A-Z0-9 ._-]{0,24}\s+vs\.?\s+[A-Z0-9][A-Z0-9 ._-]{0,24}\b", title, re.IGNORECASE):
        return False
    if not re.search(channel["includeTitle"], title, re.IGNORECASE):
        return False
    return not re.search(channel["excludeTitle"], title, re.IGNORECASE)


def catalog_metadata(channel, entry):
    pieces = [piece.strip(" -|") for piece in re.split(r"\s+(?:-|\|)\s+", clean_text(entry["title"])) if piece.strip(" -|")]
    pieces = [piece for piece in pieces if piece.upper() != "FULL MATCH"]
    title = re.sub(r"\bvs\.(?=\s|$)", "vs", pieces[0], flags=re.IGNORECASE) if pieces else "Indexed match"
    event = " | ".join(pieces[1:]) or channel["name"]
    return clean_text(title)[:100], clean_text(event)[:140]


def publishable(job, minimum_confidence, minimum_rounds):
    if job.get("status") != "ready":
        return False, job.get("message", "Indexing failed")
    if job.get("warnings"):
        return False, "; ".join(job["warnings"])
    rounds = [item for item in job.get("rounds", []) if not item.get("excluded")]
    if len(rounds) < minimum_rounds:
        return False, f"Only {len(rounds)} rounds were detected"
    if rounds[0].get("map") != 1 or rounds[0].get("round") != 1:
        return False, "The index does not begin at map 1 round 1"
    previous = None
    for round_entry in rounds:
        if round_entry.get("confidence", 0) < minimum_confidence:
            return False, "At least one round is below the automatic confidence threshold"
        if previous:
            same_map = round_entry["map"] == previous["map"] and round_entry["round"] == previous["round"] + 1
            next_map = round_entry["map"] == previous["map"] + 1 and round_entry["round"] == 1 and previous["round"] >= 12
            if not same_map and not next_map:
                return False, "The detected round sequence contains a gap"
        previous = round_entry
    return True, ""


def discover_youtube(channel, lookback, yt_dlp=None, requester=None):
    if channel.get("channelId"):
        request = requester or (lambda url: urlopen(Request(url), timeout=30).read())
        feed = request("https://www.youtube.com/feeds/videos.xml?channel_id=" + channel["channelId"])
        root = ElementTree.fromstring(feed)
        namespaces = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        entries = []
        for item in root.findall("atom:entry", namespaces)[:lookback]:
            entries.append({"id": item.findtext("yt:videoId", default="", namespaces=namespaces),
                            "title": item.findtext("atom:title", default="", namespaces=namespaces),
                            "published": item.findtext("atom:published", default="", namespaces=namespaces)})
        return [entry for entry in entries if is_candidate(channel, entry, require_duration=False)]
    options = {"extract_flat": "in_playlist", "playlistend": lookback, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(options) as downloader:
        result = downloader.extract_info(channel["url"], download=False)
    return [entry for entry in result.get("entries", []) if entry and is_candidate(channel, entry)]


def request_json(url, headers=None, data=None):
    request = Request(url, headers=headers or {}, data=data)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def twitch_access_token(client_id, client_secret, requester=request_json):
    body = urlencode({"client_id": client_id, "client_secret": client_secret,
                      "grant_type": "client_credentials"}).encode()
    result = requester("https://id.twitch.tv/oauth2/token", {"Content-Type": "application/x-www-form-urlencoded"}, body)
    token = result.get("access_token")
    if not token:
        raise ValueError("Twitch did not return an app access token")
    return token


def twitch_get(path, query, client_id, token, requester=request_json):
    url = "https://api.twitch.tv/helix/" + path + "?" + urlencode(query, doseq=True)
    return requester(url, {"Client-Id": client_id, "Authorization": "Bearer " + token}).get("data", [])


def discover_twitch(channels, lookback, client_id, client_secret, requester=request_json):
    token = twitch_access_token(client_id, client_secret, requester)
    logins = [channel["login"].lower() for channel in channels]
    users = twitch_get("users", [("login", login) for login in logins], client_id, token, requester)
    users_by_login = {user["login"].lower(): user for user in users}
    user_ids = [user["id"] for user in users]
    streams = twitch_get("streams", [("user_id", identifier) for identifier in user_ids], client_id, token, requester) if user_ids else []
    live_stream_ids = {str(stream.get("id", "")) for stream in streams}
    discovered = {}
    for channel in channels:
        user = users_by_login.get(channel["login"].lower())
        if not user:
            discovered[channel["login"]] = []
            continue
        videos = twitch_get("videos", {"user_id": user["id"], "type": "archive", "sort": "time",
                                       "first": min(100, lookback)}, client_id, token, requester)
        discovered[channel["login"]] = [entry for entry in videos
                                          if is_twitch_candidate(channel, entry, live_stream_ids)]
    return discovered


def storyboard_path(provider, identifier):
    return STORYBOARDS / f"{provider}-{identifier}.json"


def storyboard(provider, identifier, yt_dlp):
    path = storyboard_path(provider, identifier)
    if path.is_file():
        return load_storyboard(path)
    url = ("https://www.youtube.com/watch?v=" + identifier if provider == "youtube"
           else "https://www.twitch.tv/videos/" + identifier)
    value = extract_storyboard(url, provider, yt_dlp)
    save_storyboard(path, value)
    return value


def youtube_alignment(channel, entry, config, state, yt_dlp):
    target = storyboard("youtube", entry["id"], yt_dlp)
    if target["duration"] < channel["minimumDuration"]:
        raise ValueError("The YouTube upload is shorter than the configured full-match minimum")
    source_names = {item["name"] for item in config["channels"]
                    if item["provider"] == "twitch" and item.get("alignmentSource")}
    source_ids = [key.split(":", 1)[1] for key, value in state["videos"].items()
                  if key.startswith("twitch:") and value.get("status") == "published"
                  and value.get("channel") in source_names
                  and (SITE / "indexes" / f"twitch-{key.split(':', 1)[1]}.json").is_file()]
    source_ids = source_ids[-int(config.get("alignmentLookback", 8)):]
    matches = []
    for source_id in reversed(source_ids):
        try:
            reference = storyboard("twitch", source_id, yt_dlp)
            alignment = align_storyboards(reference, target)
            index = read_json(SITE / "indexes" / f"twitch-{source_id}.json")
            rounds = translate_index(index, target, alignment)
            matches.append((alignment["anchors"], source_id, alignment, rounds))
        except (OSError, ValueError, KeyError):
            continue
    if not matches:
        raise ValueError("No verified official Twitch broadcast matches this YouTube full match")
    matches.sort(reverse=True, key=lambda item: item[0])
    if len(matches) > 1 and matches[1][0] >= matches[0][0] * 0.8:
        raise ValueError("More than one official broadcast matches this YouTube upload")
    _, source_id, alignment, rounds = matches[0]
    return source_id, alignment, rounds


def process(channel, entry, config, state=None, yt_dlp=None):
    identifier = uuid.uuid4().hex
    provider = channel["provider"]
    title, event = catalog_metadata(channel, entry)
    job = {"id": identifier, "label": title, "kind": provider, "status": "queued", "progress": 0,
           "message": "Preparing the automatic index", "rounds": [], "warnings": [],
           "created": datetime.now(timezone.utc).timestamp()}
    if os.environ.get("VODLOCK_STREAM_ANALYSIS") == "1":
        job["streamAnalysis"] = True
    if provider == "twitch" and channel.get("alignmentSource"):
        job["fingerprintInterval"] = 2
    if provider == "youtube":
        job["videoId"] = entry["id"]
    else:
        job["twitchVideoId"] = entry["id"]
    server.DATA.mkdir(exist_ok=True)
    server.JOBS[identifier] = job
    server.save(job)
    aligned_source_id = None
    try:
        print(f"Processing {provider}:{entry['id']} - {clean_text(entry['title'])}", flush=True)
        if provider == "youtube" and state is not None and yt_dlp is not None:
            aligned_source_id, alignment, rounds = youtube_alignment(channel, entry, config, state, yt_dlp)
            exported = {"schemaVersion": 2, "provider": "youtube", "sourceId": entry["id"], "label": title,
                        "leadSeconds": 5, "detector": DETECTOR_VERSION + "+" + ALIGNER_VERSION,
                        "rounds": rounds, "alignment": {**alignment, "source": "twitch:" + aligned_source_id}}
        else:
            server.index_job(identifier)
            accepted, reason = publishable(job, config["minimumConfidence"], config["minimumRounds"])
            if not accepted:
                preserve_diagnostics(provider, entry["id"], server.DATA / identifier)
                return False, reason
            if provider == "twitch" and channel.get("alignmentSource"):
                save_storyboard(storyboard_path("twitch", entry["id"]),
                                {"version": 1, "provider": "twitch", "sourceId": entry["id"],
                                 "duration": round(float(job["duration"]), 3), "interval": job["fingerprintInterval"],
                                 "frames": job["fingerprints"]})
            exported = server.export(job)
            exported["rounds"] = [{"map": item["map"], "round": item["round"], "start": item["start"]}
                                  for item in exported["rounds"]]
        filename = f"{provider}-{entry['id']}.json"
        write_json(SITE / "indexes" / filename, exported)
        chat_path = None
        if provider == "twitch":
            try:
                chat_path = chat_archive.archive_chat(entry["id"])
            except Exception as error:
                print(f"twitch:{entry['id']} chat unavailable - {clean_text(error)}", file=sys.stderr)
        catalog_path = SITE / "catalog.json"
        catalog = read_json(catalog_path)
        catalog["version"] = 2
        catalog["videos"] = [item for item in catalog["videos"]
                             if source_key(item.get("provider", "youtube"), item.get("sourceId", item.get("videoId", "")))
                             != source_key(provider, entry["id"])
                             and not (aligned_source_id and item.get("provider") == "twitch"
                                      and str(item.get("sourceId", "")) == aligned_source_id)]
        catalog_entry = {"provider": provider, "sourceId": entry["id"], "title": title,
                         "event": event, "label": "Full match" if provider == "youtube" else "Full broadcast",
                         "index": f"/indexes/{filename}"}
        if chat_path:
            catalog_entry["chat"] = "/chats/" + chat_path.name
        catalog["videos"].insert(0, catalog_entry)
        catalog["updatedAt"] = datetime.now(timezone.utc).date().isoformat()
        write_json(catalog_path, catalog)
        return True, "Published"
    finally:
        server.JOBS.pop(identifier, None)
        (server.DATA / f"{identifier}.json").unlink(missing_ok=True)
        shutil.rmtree(server.DATA / identifier, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    config = read_json(CONFIG_PATH)
    state = read_json(STATE_PATH)
    catalog = read_json(SITE / "catalog.json")
    published_ids = {source_key(item.get("provider", "youtube"), item.get("sourceId", item.get("videoId", "")))
                     for item in catalog["videos"]}
    now = datetime.now(timezone.utc)
    try:
        import yt_dlp
    except ImportError as error:
        raise SystemExit("Install indexer requirements before running automatic publishing") from error
    candidates = []
    per_channel = max(1, int(config.get("maxPerChannelPerRun", 2)))
    youtube_channels = [channel for channel in config["channels"] if channel["provider"] == "youtube"]
    twitch_channels = [channel for channel in config["channels"] if channel["provider"] == "twitch"]
    for channel_index, channel in enumerate(youtube_channels):
        try:
            entries = discover_youtube(channel, config["lookback"], yt_dlp)
        except Exception as error:
            print(f"YouTube discovery failed for {channel['name']}: {error}", file=sys.stderr, flush=True)
            continue
        eligible = [item for item in entries
                    if should_attempt(source_key("youtube", item["id"]), published_ids, state["videos"],
                                      config.get("youtubeRetryHours", 0.5), now)]
        for entry_index, entry in enumerate(eligible[:per_channel]):
            key = source_key("youtube", entry["id"])
            candidates.append((1 if key in state["videos"] else 0, channel.get("priority", 1),
                               channel_index, entry_index, channel, entry))
    twitch_results = {}
    if twitch_channels:
        client_id = os.environ.get("TWITCH_CLIENT_ID", "")
        client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "")
        if client_id and client_secret:
            try:
                twitch_results = discover_twitch(twitch_channels, config["lookback"], client_id, client_secret)
            except Exception as error:
                print(f"Twitch discovery failed: {error}", file=sys.stderr, flush=True)
        else:
            print("Twitch discovery skipped: set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET", file=sys.stderr)
    for channel_index, channel in enumerate(twitch_channels):
        eligible = [item for item in twitch_results.get(channel["login"], [])
                    if should_attempt(source_key("twitch", item["id"]), published_ids, state["videos"],
                                      config.get("retryHours", 6), now)]
        for entry_index, entry in enumerate(eligible[:per_channel]):
            key = source_key("twitch", entry["id"])
            candidates.append((1 if key in state["videos"] else 0, channel.get("priority", 0),
                               channel_index, entry_index, channel, entry))
    candidates.sort(key=lambda item: item[:4])
    candidates = [(channel, entry) for *_, channel, entry in candidates[:config["maxPerRun"]]]
    if arguments.dry_run:
        for channel, entry in candidates:
            print(f"{channel['provider']}:{entry['id']} | {clean_text(entry['title'])} | {channel['name']}")
        return 0
    for channel, entry in candidates:
        key = source_key(channel["provider"], entry["id"])
        try:
            published, message = process(channel, entry, config, state, yt_dlp)
        except Exception as error:
            published, message = False, str(error)
        state["videos"][key] = {"status": "published" if published else "held",
                                "provider": channel["provider"], "channel": channel["name"],
                                "title": clean_text(entry["title"]), "message": clean_text(message),
                                "publishedAt": entry.get("published") or entry.get("created_at"),
                                "detectorVersion": DETECTOR_VERSION,
                                "pipelineVersion": PIPELINE_VERSION,
                                "retryClass": "none" if published else retry_class(message),
                                "checkedAt": datetime.now(timezone.utc).isoformat()}
        write_json(STATE_PATH, state)
        print(f"{key}: {state['videos'][key]['status']} - {message}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
