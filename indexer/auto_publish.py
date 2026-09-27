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

import server


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
CONFIG_PATH = Path(__file__).with_name("auto_channels.json")
STATE_PATH = Path(__file__).with_name("auto_state.json")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def clean_text(value):
    return re.sub(r"\s+", " ", str(value).replace("—", "-").replace("–", "-")).strip()


def source_key(provider, identifier):
    return f"{provider}:{identifier}"


def is_candidate(channel, entry):
    title = clean_text(entry.get("title", ""))
    identifier = str(entry.get("id", ""))
    duration = entry.get("duration")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
        return False
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < channel["minimumDuration"]:
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
    if twitch_duration(entry.get("duration")) < channel["minimumDuration"]:
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
    rounds = job.get("rounds", [])
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


def discover_youtube(channel, lookback, yt_dlp):
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


def process(channel, entry, config):
    identifier = uuid.uuid4().hex
    provider = channel["provider"]
    title, event = catalog_metadata(channel, entry)
    job = {"id": identifier, "label": title, "kind": provider, "status": "queued", "progress": 0,
           "message": "Preparing the automatic index", "rounds": [], "warnings": [],
           "created": datetime.now(timezone.utc).timestamp()}
    if provider == "youtube":
        job["videoId"] = entry["id"]
    else:
        job["twitchVideoId"] = entry["id"]
    server.DATA.mkdir(exist_ok=True)
    server.JOBS[identifier] = job
    server.save(job)
    try:
        server.index_job(identifier)
        accepted, reason = publishable(job, config["minimumConfidence"], config["minimumRounds"])
        if not accepted:
            return False, reason
        exported = server.export(job)
        exported["rounds"] = [{"map": item["map"], "round": item["round"], "start": item["start"]}
                              for item in exported["rounds"]]
        filename = f"{provider}-{entry['id']}.json"
        write_json(SITE / "indexes" / filename, exported)
        catalog_path = SITE / "catalog.json"
        catalog = read_json(catalog_path)
        catalog["version"] = 2
        catalog["videos"] = [item for item in catalog["videos"]
                             if source_key(item.get("provider", "youtube"), item.get("sourceId", item.get("videoId", "")))
                             != source_key(provider, entry["id"])]
        catalog["videos"].insert(0, {"provider": provider, "sourceId": entry["id"], "title": title,
                                      "event": event, "label": "Full broadcast",
                                      "index": f"/indexes/{filename}"})
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
    known = {source_key(item.get("provider", "youtube"), item.get("sourceId", item.get("videoId", "")))
             for item in catalog["videos"]} | set(state["videos"])
    try:
        import yt_dlp
    except ImportError as error:
        raise SystemExit("Install indexer requirements before running automatic publishing") from error
    candidates = []
    youtube_channels = [channel for channel in config["channels"] if channel["provider"] == "youtube"]
    twitch_channels = [channel for channel in config["channels"] if channel["provider"] == "twitch"]
    for channel in youtube_channels:
        entry = next((item for item in discover_youtube(channel, config["lookback"], yt_dlp)
                      if source_key("youtube", item["id"]) not in known), None)
        if entry:
            processed = sum(1 for item in state["videos"].values() if item.get("channel") == channel["name"])
            candidates.append((processed, channel, entry))
    twitch_results = {}
    if twitch_channels:
        client_id = os.environ.get("TWITCH_CLIENT_ID", "")
        client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "")
        if client_id and client_secret:
            twitch_results = discover_twitch(twitch_channels, config["lookback"], client_id, client_secret)
        else:
            print("Twitch discovery skipped: set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET", file=sys.stderr)
    for channel in twitch_channels:
        entry = next((item for item in twitch_results.get(channel["login"], [])
                      if source_key("twitch", item["id"]) not in known), None)
        if entry:
            processed = sum(1 for item in state["videos"].values() if item.get("channel") == channel["name"])
            candidates.append((processed, channel, entry))
    candidates.sort(key=lambda item: item[0])
    candidates = [(channel, entry) for _, channel, entry in candidates[:config["maxPerRun"]]]
    if arguments.dry_run:
        for channel, entry in candidates:
            print(f"{channel['provider']}:{entry['id']} | {clean_text(entry['title'])} | {channel['name']}")
        return 0
    for channel, entry in candidates:
        key = source_key(channel["provider"], entry["id"])
        try:
            published, message = process(channel, entry, config)
        except Exception as error:
            published, message = False, str(error)
        state["videos"][key] = {"status": "published" if published else "held",
                                "provider": channel["provider"], "channel": channel["name"],
                                "title": clean_text(entry["title"]), "message": clean_text(message),
                                "checkedAt": datetime.now(timezone.utc).isoformat()}
        write_json(STATE_PATH, state)
        print(f"{key}: {state['videos'][key]['status']} - {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
