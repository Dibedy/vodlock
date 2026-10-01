import argparse
import json
import os
import re
import shutil
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
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
PUBLISHER_VERSION = "publisher-v9"
PIPELINE_VERSION = DETECTOR_VERSION + "+" + ALIGNER_VERSION + "+" + PUBLISHER_VERSION
PUBLISH_LOCK = threading.Lock()


class OfficialMatchPending(ValueError):
    pass


class OfficialArchiveUnmatched(ValueError):
    pass


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
        "A round sequence was detected before round 1",
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


def recovery_message(channel, error):
    message = str(error)
    if channel["provider"] == "youtube" and "confirm you’re not a bot" in message:
        if os.environ.get("VODLOCK_YOUTUBE_COOKIES"):
            return "YouTube rejected the configured cookies. Refresh the YOUTUBE_COOKIES secret and retry."
        return "YouTube blocked GitHub's shared runner. Add the YOUTUBE_COOKIES secret and retry."
    return message


def should_attempt(key, published, state, retry_hours, now):
    if key in published:
        return False
    previous = state.get(key)
    if not previous:
        return True
    if previous.get("status") in {"superseded", "indexed"}:
        return False
    if previous.get("status") == "waiting":
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


def should_process(key, published, state, retry_hours, now, retry_held=False):
    if retry_held:
        return key not in published and state.get(key, {}).get("status") in {"held", "waiting"}
    return should_attempt(key, published, state, retry_hours, now)


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


def tournament_metadata(event, title=""):
    tournament = clean_text(event)
    if re.match(r"^[^|]{1,80}\bvs\.?\s+[^|]{1,80}\|", tournament, re.IGNORECASE):
        tournament = tournament.split("|", 1)[1].strip()
    tournament = re.sub(r"\s*[|·-]\s*(?:opening day|group stage|swiss stage|playoffs?|upper final|lower final|grand final).*?$", "", tournament, flags=re.IGNORECASE)
    tournament = re.sub(r"\s+(?:opening day|group stage|swiss stage|playoffs?|upper final|lower final|grand final).*?$", "", tournament, flags=re.IGNORECASE)
    tournament = tournament.strip(" -|") or "Tournament archive"
    key = re.sub(r"[^a-z0-9]+", "-", tournament.lower()).strip("-") or "tournament-archive"
    return {"tournament": tournament[:100], "tournamentKey": key[:100]}


def catalog_played_at(entry, rounds, source=None, alignment=None):
    value = source.get("publishedAt") if source else entry.get("created_at") or entry.get("published")
    if not value or not rounds:
        raise ValueError("The match does not have enough timing information for the catalog")
    started = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    first_round = float(rounds[0]["start"])
    elapsed = first_round
    if alignment:
        segments = alignment.get("segments") or [{"offset": alignment["offset"], "targetStart": 0,
                                                    "targetEnd": float("inf")}]
        segment = next((item for item in segments
                        if float(item.get("targetStart", 0)) <= first_round < float(item.get("targetEnd", float("inf")))),
                       segments[0])
        elapsed = float(alignment.get("timelineScale", 1)) * first_round + float(segment["offset"])
    played = started.astimezone(timezone.utc) + timedelta(seconds=elapsed)
    return played.isoformat(timespec="seconds").replace("+00:00", "Z")


def pipeline_summary(state, config=None):
    videos = state.get("videos", {})
    if config:
        channels = {item["name"]: item for item in config["channels"]}

        def tracked(item):
            channel = channels.get(item.get("channel"))
            if not channel:
                return False
            title = clean_text(item.get("title", ""))
            return bool(re.search(channel["includeTitle"], title, re.IGNORECASE)
                        and not re.search(channel["excludeTitle"], title, re.IGNORECASE))

        videos = {key: item for key, item in videos.items() if tracked(item)}
    counts = {status: sum(1 for item in videos.values() if item.get("status") == status)
              for status in ("published", "indexed", "waiting", "held", "superseded")}
    recent = sorted(videos.items(), key=lambda item: item[1].get("checkedAt", ""), reverse=True)[:20]

    def cell(value):
        return clean_text(value).replace("|", "\\|")

    lines = ["## VOD pipeline health", "",
             f"Published: **{counts['published']}** · Indexed: **{counts['indexed']}** · Waiting: **{counts['waiting']}** · Held: **{counts['held']}** · Superseded: **{counts['superseded']}**",
             "", "| Source | Status | Channel | Last checked | Result |", "|---|---|---|---|---|"]
    for key, item in recent:
        lines.append(f"| `{cell(key)}` | {cell(item.get('status', 'unknown'))} | {cell(item.get('channel', ''))} | "
                     f"{cell(item.get('checkedAt', ''))} | {cell(item.get('message', ''))} |")
    lines.extend(["", "Use **Run workflow → Retry held sources** to retry one eligible held source or recheck a waiting dependency."])
    return "\n".join(lines) + "\n"


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


def sequence_warnings(rounds):
    rounds = [item for item in rounds if not item.get("excluded")]
    warnings = []
    for previous, item in zip(rounds, rounds[1:]):
        same_map = item["map"] == previous["map"] and item["round"] == previous["round"] + 1
        next_map = item["map"] == previous["map"] + 1 and item["round"] == 1 and previous["round"] >= 12
        if not same_map and not next_map:
            warnings.append(f"Map {item['map']}: check the gap before round {item['round']}.")
    return warnings


def isolated_gap(job):
    rounds = [item for item in job.get("rounds", []) if not item.get("excluded")]
    gaps = [(previous, item) for previous, item in zip(rounds, rounds[1:])
            if item["map"] == previous["map"] and item["round"] > previous["round"] + 1]
    if len(job.get("warnings", [])) == 1 and len(gaps) == 1:
        return gaps[0]
    return None


def merge_gap_repair(original_rounds, repaired_rounds, gap):
    previous, following = gap
    replacements = [item for item in repaired_rounds
                    if item["map"] == previous["map"]
                    and previous["round"] < item["round"] < following["round"]
                    and previous["start"] < item["start"] < following["start"]]
    merged = [dict(item) for item in original_rounds]
    existing = {(item["map"], item["round"]) for item in merged}
    merged.extend(dict(item) for item in replacements if (item["map"], item["round"]) not in existing)
    merged.sort(key=lambda item: item["start"])
    return merged


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
        value = load_storyboard(path)
        if value.get("version") >= 2:
            return value
    url = ("https://www.youtube.com/watch?v=" + identifier if provider == "youtube"
           else "https://www.twitch.tv/videos/" + identifier)
    value = extract_storyboard(url, provider, yt_dlp)
    save_storyboard(path, value)
    return value


def normalize_storyboard_timeline(value):
    frames = value.get("frames", [])
    duration = float(value.get("duration", 0))
    interval = float(value.get("interval", 0))
    if not frames or duration <= 0 or interval <= 0:
        return value, 1.0
    scale = (float(frames[-1]["time"]) + interval) / duration
    if not 1.5 <= scale <= 2.2:
        return value, 1.0
    return {**value, "frames": [{**item, "time": round(float(item["time"]) / scale, 3)} for item in frames]}, scale


def youtube_alignment(channel, entry, config, state, yt_dlp):
    target = storyboard("youtube", entry["id"], yt_dlp)
    if target["duration"] < channel["minimumDuration"]:
        raise ValueError("The YouTube upload is shorter than the configured full-match minimum")
    source_names = {item["name"] for item in config["channels"]
                    if item["provider"] == "twitch" and item.get("alignmentSource")}
    source_ids = [key.split(":", 1)[1] for key, value in state["videos"].items()
                  if key.startswith("twitch:") and value.get("status") in {"published", "superseded", "indexed"}
                  and value.get("channel") in source_names
                  and (SITE / "indexes" / f"twitch-{key.split(':', 1)[1]}.json").is_file()]
    source_ids = source_ids[-int(config.get("alignmentLookback", 8)):]
    matches = []
    for source_id in reversed(source_ids):
        try:
            reference = storyboard("twitch", source_id, yt_dlp)
            reference, timebase_scale = normalize_storyboard_timeline(reference)
            reference = compact_reference(reference, target.get("interval", 10))
            alignment = align_storyboards(reference, target)
            index = read_json(SITE / "indexes" / f"twitch-{source_id}.json")
            if timebase_scale != 1:
                index = {**index, "rounds": [{**item, "start": float(item["start"]) / timebase_scale}
                                               for item in index["rounds"]]}
            rounds = translate_index(index, target, alignment)
            matches.append((alignment["anchors"], source_id, alignment, rounds))
        except (OSError, ValueError, KeyError):
            continue
    if not matches:
        raise OfficialArchiveUnmatched("No verified official Twitch broadcast matches this YouTube full match")
    matches.sort(reverse=True, key=lambda item: item[0])
    if len(matches) > 1 and matches[1][0] >= matches[0][0] * 0.8:
        raise ValueError("More than one official broadcast matches this YouTube upload")
    _, source_id, alignment, rounds = matches[0]
    return source_id, alignment, rounds


def matchup_key(value):
    match = re.search(r"\b([A-Z0-9][A-Z0-9 .']{0,24}?)\s+vs\.?\s+([A-Z0-9][A-Z0-9 .']{0,24}?)(?=\s*[-|#]|$)",
                      clean_text(value), re.IGNORECASE)
    if not match:
        return ""
    teams = [re.sub(r"[^A-Z0-9]+", "", team.upper()) for team in match.groups()]
    return ":".join(sorted(teams)) if all(teams) else ""


def reset_analysis_job(job):
    job.update(status="queued", progress=0, message="Preparing the automatic index", rounds=[], warnings=[],
               fingerprints=[])


def compact_reference(reference, interval):
    source_interval = max(1, float(reference.get("interval", interval)))
    step = max(1, round(interval / source_interval))
    return {**reference, "interval": source_interval * step, "frames": reference.get("frames", [])[::step]}


def watchparty_alignments(job, entry, config, state, yt_dlp, multi_series=False):
    target_match = matchup_key(entry.get("title", ""))
    if not multi_series and not target_match:
        raise ValueError("The watch-party title does not identify a matchup for alignment")
    candidates = [(key.split(":", 1)[1], value) for key, value in state["videos"].items()
                  if key.startswith("youtube:") and value.get("status") == "published"
                  and (multi_series or matchup_key(value.get("title", "")) == target_match)
                  and (SITE / "indexes" / f"youtube-{key.split(':', 1)[1]}.json").is_file()]
    if not candidates:
        raise OfficialMatchPending("Waiting for the indexed official YouTube full match")
    interval = int(config.get("watchPartyFingerprintInterval", 10))
    job.update(fingerprintOnly=True, fingerprintInterval=interval, analysisHeight=540)
    server.index_job(job["id"])
    if job.get("status") != "ready" or len(job.get("fingerprints", [])) < 30:
        raise ValueError(job.get("message", "The watch-party fingerprint could not be created"))
    target = {"version": 2, "provider": "twitch", "sourceId": entry["id"],
              "duration": round(float(job["duration"]), 3), "interval": interval,
              "frames": job["fingerprints"]}
    matches = []
    failures = []
    for source_id, _ in reversed(candidates[-int(config.get("alignmentLookback", 8)):]):
        try:
            reference = storyboard("youtube", source_id, yt_dlp)
            alignment = align_storyboards(reference, target,
                                          maximum_distance=int(config.get("watchPartyMaximumDistance", 18)),
                                          require_target_coverage=False)
            index = read_json(SITE / "indexes" / f"youtube-{source_id}.json")
            rounds = translate_index(index, target, alignment)
            matches.append((alignment["anchors"], source_id, alignment, rounds))
        except (OSError, ValueError, KeyError) as error:
            failures.append({"sourceId": source_id, "reason": clean_text(error)})
    if not matches:
        DIAGNOSTICS.mkdir(exist_ok=True)
        write_json(DIAGNOSTICS / f"watchparty-{entry['id']}.json",
                   {"version": 1, "watchPartyId": entry["id"], "candidates": failures})
        raise ValueError("No verified official-broadcast alignment was found for this watch party")
    matches.sort(reverse=True, key=lambda item: item[0])
    if not multi_series and len(matches) > 1 and matches[1][0] >= matches[0][0] * 0.8:
        raise ValueError("More than one official broadcast matches this watch party")
    return [(source_id, alignment, rounds) for _, source_id, alignment, rounds in matches]


def watchparty_alignment(job, entry, config, state, yt_dlp):
    matches = watchparty_alignments(job, entry, config, state, yt_dlp)
    source_id, alignment, rounds = matches[0]
    return "youtube:" + source_id, alignment, rounds


def publish_watchparty_archive(channel, entry, state, matches):
    chat_path = None
    try:
        chat_path = chat_archive.archive_chat(entry["id"])
    except Exception as error:
        print(f"twitch:{entry['id']} chat unavailable - {clean_text(error)}", file=sys.stderr)
    catalog_path = SITE / "catalog.json"
    catalog = read_json(catalog_path)
    catalog["version"] = 2
    published = {item.get("sourceId"): item for item in catalog["videos"] if item.get("provider") == "youtube"}
    records = []
    for source_id, alignment, rounds in matches:
        official = published.get(source_id, {})
        title = clean_text(official.get("title") or state["videos"]["youtube:" + source_id]["title"])
        event = clean_text(official.get("event") or channel["name"])
        catalog_id = f"twitch:{entry['id']}:{source_id}"
        exported = {"schemaVersion": 2, "provider": "twitch", "sourceId": entry["id"], "label": title,
                    "leadSeconds": 5, "detector": DETECTOR_VERSION + "+" + ALIGNER_VERSION,
                    "rounds": rounds, "alignment": {**alignment, "source": "youtube:" + source_id}}
        filename = f"twitch-{entry['id']}-{source_id}.json"
        write_json(SITE / "indexes" / filename, exported)
        catalog["videos"] = [item for item in catalog["videos"] if item.get("catalogId") != catalog_id]
        record = {"provider": "twitch", "sourceId": entry["id"], "catalogId": catalog_id,
                  "kind": "watch-party", "creator": channel["name"].replace(" on Twitch", ""),
                  "title": title, "event": event, "label": "Watch party", "index": f"/indexes/{filename}",
                  "playedAt": official.get("playedAt") or catalog_played_at(entry, rounds),
                  **({key: official[key] for key in ("tournament", "tournamentKey") if key in official} or tournament_metadata(event, title))}
        if chat_path:
            record["chat"] = "/chats/" + chat_path.name
        records.append(record)
    catalog["videos"].extend(records)
    catalog["videos"].sort(key=lambda item: item.get("playedAt", ""), reverse=True)
    catalog["updatedAt"] = datetime.now(timezone.utc).date().isoformat()
    write_json(catalog_path, catalog)
    return True, f"Published {len(records)} complete matches from the watch-party archive"


def process(channel, entry, config, state=None, yt_dlp=None):
    identifier = uuid.uuid4().hex
    provider = channel["provider"]
    title, event = catalog_metadata(channel, entry)
    job = {"id": identifier, "label": title, "kind": provider, "status": "queued", "progress": 0,
           "message": "Preparing the automatic index", "rounds": [], "warnings": [],
           "created": datetime.now(timezone.utc).timestamp()}
    if os.environ.get("VODLOCK_STREAM_ANALYSIS") == "1":
        job["streamAnalysis"] = True
    if provider == "twitch":
        job["adaptiveAnalysis"] = True
        job["analysisHeight"] = 540
        if channel.get("archiveOnly"):
            job["multiSeriesArchive"] = True
    if provider == "youtube":
        job["adaptiveAnalysis"] = True
        job["analysisHeight"] = 540
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
    alignment = None
    try:
        print(f"Processing {provider}:{entry['id']} - {clean_text(entry['title'])}", flush=True)
        if provider == "youtube" and state is not None and yt_dlp is not None:
            try:
                aligned_source_id, alignment, rounds = youtube_alignment(channel, entry, config, state, yt_dlp)
                exported = {"schemaVersion": 2, "provider": "youtube", "sourceId": entry["id"], "label": title,
                            "leadSeconds": 5, "detector": DETECTOR_VERSION + "+" + ALIGNER_VERSION,
                            "rounds": rounds, "alignment": {**alignment, "source": "twitch:" + aligned_source_id}}
            except OfficialArchiveUnmatched as error:
                print(f"youtube:{entry['id']} archive alignment unavailable - {clean_text(error)}; using adaptive official OCR",
                      flush=True)
                job["streamAnalysis"] = False
        if aligned_source_id is None:
            if state is not None and yt_dlp is not None and channel.get("reuseOfficialIndex"):
                try:
                    if channel.get("multiSeriesArchive"):
                        matches = watchparty_alignments(job, entry, config, state, yt_dlp, multi_series=True)
                        with PUBLISH_LOCK:
                            return publish_watchparty_archive(channel, entry, state, matches)
                    aligned_source_id, alignment, rounds = watchparty_alignment(job, entry, config, state, yt_dlp)
                    exported = {"schemaVersion": 2, "provider": "twitch", "sourceId": entry["id"], "label": title,
                                "leadSeconds": 5, "detector": DETECTOR_VERSION + "+" + ALIGNER_VERSION,
                                "rounds": rounds, "alignment": {**alignment, "source": aligned_source_id if ":" in aligned_source_id else "twitch:" + aligned_source_id}}
                except OfficialMatchPending as error:
                    return "waiting", str(error)
                except (OSError, ValueError, KeyError) as error:
                    return False, "Official match alignment failed: " + clean_text(error)
            if aligned_source_id is None:
                server.index_job(identifier)
                accepted, reason = publishable(job, config["minimumConfidence"], config["minimumRounds"])
                if not accepted and job.get("adaptiveAnalysis") and job.get("status") == "ready":
                    gap = isolated_gap(job)
                    if gap:
                        original_rounds = [dict(item) for item in job["rounds"]]
                        original_fingerprints = list(job.get("fingerprints", []))
                        previous, following = gap
                        analysis_start = max(0, float(previous["start"]) - 30)
                        analysis_end = min(float(job["duration"]), float(following["start"]) + 30)
                        print(f"{provider}:{entry['id']} checking {analysis_start:.0f}-{analysis_end:.0f}s at 720p to repair one gap",
                              flush=True)
                        job["adaptiveAnalysis"] = False
                        job["analysisHeight"] = 720
                        job["analysisWindow"] = [analysis_start, analysis_end]
                        job["seedRound"] = dict(previous)
                        reset_analysis_job(job)
                        server.index_job(identifier)
                        repaired_rounds = list(job.get("rounds", []))
                        merged = merge_gap_repair(original_rounds, repaired_rounds, gap)
                        job.update(status="ready", rounds=merged, warnings=sequence_warnings(merged),
                                   fingerprints=original_fingerprints)
                        accepted, reason = publishable(job, config["minimumConfidence"], config["minimumRounds"])
                    else:
                        print(f"{provider}:{entry['id']} adaptive analysis broadly unreliable - {clean_text(reason)}; retrying full 720p OCR",
                              flush=True)
                        job["adaptiveAnalysis"] = False
                        job["analysisHeight"] = 720
                        reset_analysis_job(job)
                        server.index_job(identifier)
                        accepted, reason = publishable(job, config["minimumConfidence"], config["minimumRounds"])
                if not accepted:
                    preserve_diagnostics(provider, entry["id"], server.DATA / identifier)
                    return False, reason
                exported = server.export(job)
                exported["rounds"] = [{"map": item["map"], "round": item["round"], "start": item["start"]}
                                      for item in exported["rounds"]]
        filename = f"{provider}-{entry['id']}.json"
        write_json(SITE / "indexes" / filename, exported)
        if provider == "twitch" and channel.get("alignmentSource"):
            save_storyboard(storyboard_path("twitch", entry["id"]),
                            {"version": 2, "provider": "twitch", "sourceId": entry["id"],
                             "duration": round(float(job["duration"]), 3), "interval": job["fingerprintInterval"],
                             "frames": job["fingerprints"]})
        if channel.get("archiveOnly"):
            return "indexed", "Indexed official day broadcast"
        chat_path = None
        if provider == "twitch":
            try:
                chat_path = chat_archive.archive_chat(entry["id"])
            except Exception as error:
                print(f"twitch:{entry['id']} chat unavailable - {clean_text(error)}", file=sys.stderr)
        elif aligned_source_id:
            try:
                existing_chat = SITE / "chats" / f"twitch-{aligned_source_id}.json"
                chat_path = existing_chat if existing_chat.is_file() else chat_archive.archive_chat(aligned_source_id)
            except Exception as error:
                print(f"twitch:{aligned_source_id} chat unavailable - {clean_text(error)}", file=sys.stderr)
        supersede_source = provider == "youtube" and aligned_source_id and state is not None \
            and state["videos"].get(source_key("twitch", aligned_source_id), {}).get("status") in {"published", "superseded"}
        with PUBLISH_LOCK:
            catalog_path = SITE / "catalog.json"
            catalog = read_json(catalog_path)
            catalog["version"] = 2
            catalog["videos"] = [item for item in catalog["videos"]
                                 if source_key(item.get("provider", "youtube"), item.get("sourceId", item.get("videoId", "")))
                                 != source_key(provider, entry["id"])
                                 and not (supersede_source and item.get("provider") == "twitch"
                                          and str(item.get("sourceId", "")) == aligned_source_id)]
            source = (state["videos"].get(source_key("twitch", aligned_source_id))
                      if provider == "youtube" and aligned_source_id and state is not None else None)
            played_at = catalog_played_at(entry, exported["rounds"], source,
                                          alignment if source else None)
            catalog_entry = {"provider": provider, "sourceId": entry["id"], "title": title,
                             "event": event, "label": "Full match" if provider == "youtube" else "Full broadcast",
                             "index": f"/indexes/{filename}", "playedAt": played_at, **tournament_metadata(event, title)}
            if chat_path:
                catalog_entry["chat"] = "/chats/" + chat_path.name
                if provider == "youtube":
                    catalog_entry["chatSourceId"] = aligned_source_id
            catalog["videos"].insert(0, catalog_entry)
            catalog["videos"].sort(key=lambda item: item.get("playedAt", ""), reverse=True)
            catalog["updatedAt"] = datetime.now(timezone.utc).date().isoformat()
            write_json(catalog_path, catalog)
            if supersede_source and source is not None:
                source["status"] = "superseded"
                source["message"] = "Superseded by " + source_key("youtube", entry["id"])
                source["supersededBy"] = source_key("youtube", entry["id"])
        return True, "Published"
    finally:
        server.JOBS.pop(identifier, None)
        (server.DATA / f"{identifier}.json").unlink(missing_ok=True)
        shutil.rmtree(server.DATA / identifier, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-held", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    arguments = parser.parse_args()
    config = read_json(CONFIG_PATH)
    state = read_json(STATE_PATH)
    if arguments.summary_only:
        summary = pipeline_summary(state, config)
        destination = os.environ.get("GITHUB_STEP_SUMMARY")
        if destination:
            Path(destination).write_text(summary, encoding="utf-8")
        else:
            sys.stdout.buffer.write(summary.encode("utf-8"))
        return 0
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
        known_ids = {item["id"] for item in entries}
        retained = [
            {"id": key.split(":", 1)[1], "title": item["title"], "published": item.get("publishedAt")}
            for key, item in state["videos"].items()
            if key.startswith("youtube:") and item.get("status") == "held"
            and item.get("channel") == channel["name"] and key.split(":", 1)[1] not in known_ids
        ]
        entries.extend(retained)
        eligible = [item for item in entries
                    if should_process(source_key("youtube", item["id"]), published_ids, state["videos"],
                                      config.get("youtubeRetryHours", 0.5), now, arguments.retry_held)]
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
        entries = list(twitch_results.get(channel["login"], []))
        known_ids = {item["id"] for item in entries}
        retained = [
            {"id": key.split(":", 1)[1], "title": item["title"], "created_at": item.get("publishedAt")}
            for key, item in state["videos"].items()
            if key.startswith("twitch:") and item.get("status") in {"held", "waiting"}
            and item.get("channel") == channel["name"] and key.split(":", 1)[1] not in known_ids
        ]
        entries.extend(retained)
        eligible = [item for item in entries
                    if should_process(source_key("twitch", item["id"]), published_ids, state["videos"],
                                      config.get("retryHours", 6), now, arguments.retry_held)]
        for entry_index, entry in enumerate(eligible[:per_channel]):
            key = source_key("twitch", entry["id"])
            candidates.append((1 if key in state["videos"] else 0, channel.get("priority", 0),
                               channel_index, entry_index, channel, entry))
    candidates.sort(key=lambda item: item[:4])
    alignment_sources = {channel["name"] for channel in config["channels"] if channel.get("alignmentSource")}
    has_alignment_archive = any(channel.get("alignmentSource") for *_, channel, _ in candidates) or any(
        item.get("status") == "indexed" and item.get("channel") in alignment_sources
        for item in state["videos"].values())
    selected = []
    retry_count = 0
    for candidate in candidates:
        retrying = candidate[0]
        channel = candidate[-2]
        dependent = has_alignment_archive and (channel.get("alignmentSource") or channel.get("reuseOfficialIndex")
                                                or channel["provider"] == "youtube")
        if retrying and retry_count >= config.get("maxRetriesPerRun", 1) and not dependent:
            continue
        selected.append(candidate)
        retry_count += retrying and not dependent
        if len(selected) >= config["maxPerRun"]:
            break
    candidates = [(channel, entry) for *_, channel, entry in selected]
    if arguments.dry_run:
        for channel, entry in candidates:
            print(f"{channel['provider']}:{entry['id']} | {clean_text(entry['title'])} | {channel['name']}")
        return 0

    def run_candidate(channel, entry):
        try:
            published, message = process(channel, entry, config, state, yt_dlp)
        except Exception as error:
            published, message = False, recovery_message(channel, error)
        return channel, entry, published, message

    def record_result(result):
        channel, entry, published, message = result
        key = source_key(channel["provider"], entry["id"])
        status = published if published in {"indexed", "published", "waiting", "held", "superseded"} else ("published" if published else "held")
        state["videos"][key] = {"status": status,
                                "provider": channel["provider"], "channel": channel["name"],
                                "title": clean_text(entry["title"]), "message": clean_text(message),
                                "publishedAt": entry.get("published") or entry.get("created_at"),
                                "detectorVersion": DETECTOR_VERSION,
                                "pipelineVersion": PIPELINE_VERSION,
                                "retryClass": ("none" if status in {"published", "indexed", "superseded"}
                                               else "dependency" if status == "waiting" else retry_class(message)),
                                "checkedAt": datetime.now(timezone.utc).isoformat()}
        write_json(STATE_PATH, state)
        print(f"{key}: {state['videos'][key]['status']} - {message}", flush=True)

    prerequisites = [(channel, entry) for channel, entry in candidates if channel.get("alignmentSource")]
    youtube_candidates = [(channel, entry) for channel, entry in candidates
                          if has_alignment_archive and channel["provider"] == "youtube"]
    remaining = [(channel, entry) for channel, entry in candidates
                 if not channel.get("alignmentSource") and (not has_alignment_archive or channel["provider"] != "youtube")]
    for channel, entry in prerequisites:
        record_result(run_candidate(channel, entry))
    for channel, entry in youtube_candidates:
        record_result(run_candidate(channel, entry))
    workers = min(max(1, int(config.get("maxWorkers", 1))), len(remaining)) if remaining else 0
    if workers == 1:
        for channel, entry in remaining:
            record_result(run_candidate(channel, entry))
    elif workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(run_candidate, channel, entry) for channel, entry in remaining]
            for future in as_completed(futures):
                record_result(future.result())
    return 0


if __name__ == "__main__":
    sys.exit(main())
