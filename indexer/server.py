import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from detector import DETECTOR_VERSION, HudReader, RoundDetector
from storyboard_align import frame_hash

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PORT = 8766
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
CANCEL = threading.Event()
JOBS = {}
ACTIVE = None
ANALYSIS_MAX_BYTES = 8 * 1024 ** 3
ANALYSIS_RESERVE_BYTES = 2 * 1024 ** 3
MINIMUM_ANALYSIS_BYTES = 512 * 1024 ** 2
INITIAL_DENSE_SAMPLE_SECONDS = 15 * 60


class AnalysisSizeLimitError(ValueError):
    pass


def analysis_download_limit(work):
    configured = os.environ.get("VODLOCK_ANALYSIS_MAX_GB", "")
    maximum = ANALYSIS_MAX_BYTES
    if configured:
        try:
            maximum = min(maximum, int(float(configured) * 1024 ** 3))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("VODLOCK_ANALYSIS_MAX_GB must be a positive number.")
    available = shutil.disk_usage(work).free - ANALYSIS_RESERVE_BYTES
    limit = min(maximum, available)
    if maximum <= 0 or limit < MINIMUM_ANALYSIS_BYTES:
        raise AnalysisSizeLimitError("Not enough free disk space remains for a safe analysis copy.")
    return limit


def size_limit_message(max_bytes):
    return f"The selected analysis copy was not downloaded. It may exceed the {max_bytes / 1024 ** 3:g} GB limit."


def video_id(value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {"youtube.com", "www.youtube.com", "youtu.be"}:
        raise ValueError("Use a public HTTPS YouTube video link.")
    if parsed.hostname == "youtu.be":
        identifier = parsed.path.strip("/")
    elif parsed.path == "/watch":
        identifier = parse_qs(parsed.query).get("v", [""])[0]
    else:
        raise ValueError("Use the video's watch link, not a channel or playlist.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
        raise ValueError("This YouTube link does not contain a valid video ID.")
    return identifier


def twitch_video_id(value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {"twitch.tv", "www.twitch.tv"}:
        raise ValueError("Use a public HTTPS Twitch VOD link.")
    match = re.fullmatch(r"/videos/([0-9]{6,20})/?", parsed.path)
    if not match:
        raise ValueError("Use a finished Twitch VOD link, not a channel or live stream.")
    return match.group(1)


def save(job):
    destination = DATA / (job["id"] + ".json")
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(job, indent=2), encoding="utf-8")
    temporary.replace(destination)


def update(identifier, **values):
    with LOCK:
        job = JOBS[identifier]
        job.update(values)
        save(job)


def export(job):
    provider = "twitch" if job.get("kind") == "twitch" else "youtube"
    source_id = job.get("twitchVideoId", "") if provider == "twitch" else job.get("videoId", "")
    return {"schemaVersion": 2, "provider": provider, "sourceId": source_id, "label": job["label"],
            "leadSeconds": 5, "detector": DETECTOR_VERSION,
            "rounds": [entry for entry in job.get("rounds", []) if not entry.get("excluded")]}


def check_cancel():
    if CANCEL.is_set():
        raise InterruptedError("Indexing cancelled. Your original video has not been changed.")


def remote_formats(job):
    formats = [("bestvideo[height<=720]/best[height<=720]", None),
               ("bestvideo[height<=540]/best[height<=540]", None),
               ("bestvideo[height<=360]/best[height<=360]", None)]
    if job.get("kind") == "twitch":
        formats[0] = ("bestvideo[height=720][fps<=30]/best[height=720][fps<=30]/bestvideo[height<=720]/best[height<=720]", None)
    if job.get("kind", "youtube") == "youtube" and os.environ.get("VODLOCK_YOUTUBE_POT") == "1":
        formats = [("bestvideo[height<=720]/best[height<=720]", None),
                   ("bestvideo[height<=720]/best[height<=720]", "mweb"),
                   ("bestvideo[height<=720][protocol=m3u8_native]/best[height<=720][protocol=m3u8_native]", "web_safari"),
                   ("bestvideo[height<=720]/best[height<=720]", "web_embedded")]
    return formats


def remote_url(job):
    if job.get("kind", "youtube") == "youtube":
        return "https://www.youtube.com/watch?v=" + job["videoId"]
    return "https://www.twitch.tv/videos/" + job["twitchVideoId"]


def resolve_remote(job, yt_dlp):
    options = {"noplaylist": True, "quiet": True, "no_warnings": False, "socket_timeout": 20, "retries": 2}
    if shutil.which("node"):
        options["js_runtimes"] = {"node": {"path": shutil.which("node")}}
    last_error = None
    for format_selector, player_client in remote_formats(job):
        options["format"] = format_selector
        if player_client:
            options["extractor_args"] = {"youtube": {"player_client": [player_client]}}
        else:
            options.pop("extractor_args", None)
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(remote_url(job), download=False)
            media_url = info.get("url")
            if not media_url:
                raise yt_dlp.utils.DownloadError("The video service did not return a playable media URL")
            duration = float(info.get("duration", 0))
            if not math.isfinite(duration) or duration <= 0:
                raise yt_dlp.utils.DownloadError("The video service did not return a valid duration")
            headers = {str(key): str(value) for key, value in info.get("http_headers", {}).items()
                       if "\r" not in str(key) + str(value) and "\n" not in str(key) + str(value)}
            return {"url": media_url, "duration": duration, "headers": headers,
                    "format": format_selector, "playerClient": player_client}
        except yt_dlp.utils.DownloadError as error:
            last_error = error
    raise last_error


def download_remote(job, work, hook, yt_dlp, max_bytes=None):
    max_bytes = max_bytes or analysis_download_limit(work)
    options = {"noplaylist": True, "outtmpl": str(work / "source.%(ext)s"), "quiet": True,
               "no_warnings": False, "noprogress": True, "progress_hooks": [hook], "max_filesize": max_bytes,
               "socket_timeout": 20, "retries": 2, "concurrent_fragment_downloads": 8}
    if shutil.which("node"):
        options["js_runtimes"] = {"node": {"path": shutil.which("node")}}
    last_error = None
    formats = remote_formats(job)
    for attempt, (format_selector, player_client) in enumerate(formats):
        options["format"] = format_selector
        if player_client:
            options["extractor_args"] = {"youtube": {"player_client": [player_client]}}
        else:
            options.pop("extractor_args", None)
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(remote_url(job), download=True)
                source = Path(downloader.prepare_filename(info))
                if not source.is_file():
                    raise AnalysisSizeLimitError(size_limit_message(max_bytes))
                return source
        except (yt_dlp.utils.DownloadError, AnalysisSizeLimitError) as error:
            last_error = error
            for path in work.glob("source.*"):
                path.unlink(missing_ok=True)
            if attempt + 1 < len(formats):
                update(job["id"], message="The video service rejected one playback client · trying another", progress=0)
    raise last_error


def download_youtube(job, work, hook, yt_dlp):
    return download_remote(job, work, hook, yt_dlp)


def stream_command(job, remote):
    command = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-progress",
               "--no-part", "--format", remote["format"], "--output", "-", remote_url(job)]
    if remote.get("playerClient"):
        command[3:3] = ["--extractor-args", "youtube:player_client=" + remote["playerClient"]]
    return command


def index_job(identifier):
    global ACTIVE
    process = None
    stream = None
    try:
        import cv2
        import imageio_ffmpeg
        import numpy as np
        import yt_dlp
        job = JOBS[identifier]
        work = DATA / identifier
        work.mkdir(exist_ok=True)
        remote = None
        if job["kind"] in {"youtube", "twitch"} and job.get("streamAnalysis"):
            update(identifier, status="downloading", message="Opening the remote analysis stream · your viewing player is untouched", progress=0)
            remote = resolve_remote(job, yt_dlp)
            source = "pipe:0"
        elif job["kind"] in {"youtube", "twitch"}:
            update(identifier, status="downloading", message="Downloading an analysis copy · your viewing player is untouched", progress=0)
            max_bytes = analysis_download_limit(work)

            def hook(event):
                check_cancel()
                if event["status"] == "downloading":
                    if event.get("downloaded_bytes", 0) > max_bytes:
                        raise AnalysisSizeLimitError(size_limit_message(max_bytes))
                    total = event.get("total_bytes") or event.get("total_bytes_estimate")
                    percent = min(35, int(event.get("downloaded_bytes", 0) / total * 35)) if total else 0
                    with LOCK:
                        job["progress"] = percent

            source = download_remote(job, work, hook, yt_dlp, max_bytes)
            if not source.is_file():
                raise ValueError(size_limit_message(max_bytes) + " It may also need sign-in. Use a local copy instead.")
        else:
            source = Path(job["source"])
        check_cancel()
        update(identifier, status="analyzing", message="Reading the broadcast clock and rejecting replay frames", progress=35)
        reader = HudReader()
        detector = RoundDetector()
        fingerprint_interval = int(job.get("fingerprintInterval", 0))
        fingerprints = []
        diagnostic_directory = work / "diagnostics"
        diagnostic_count = 0
        if remote:
            duration = remote["duration"]
        else:
            capture = cv2.VideoCapture(str(source))
            duration = capture.get(cv2.CAP_PROP_FRAME_COUNT) / max(1, capture.get(cv2.CAP_PROP_FPS))
            opened = capture.isOpened()
            capture.release()
            if not opened or not math.isfinite(duration) or duration <= 0:
                raise ValueError("Could not read this video. Try an MP4, MKV or WebM recording.")
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(source),
                   "-an", "-vf", "fps=fps=1:start_time=0:round=up,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                   "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        error_path = work / "ffmpeg.log"
        stream_error_path = work / "stream.log"
        with error_path.open("wb") as error_log, stream_error_path.open("wb") as stream_error_log:
            if remote:
                stream = subprocess.Popen(stream_command(job, remote), stdout=subprocess.PIPE, stderr=stream_error_log,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            process = subprocess.Popen(command, stdin=stream.stdout if stream else None,
                                       stdout=subprocess.PIPE, stderr=error_log,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if stream and stream.stdout:
                stream.stdout.close()
            frame_number = 0
            frame_size = 1280 * 720 * 3
            while True:
                check_cancel()
                chunk = bytearray()
                while len(chunk) < frame_size:
                    data = process.stdout.read(frame_size - len(chunk))
                    if not data:
                        break
                    chunk.extend(data)
                if not chunk:
                    break
                if len(chunk) != frame_size:
                    raise ValueError("Video decoding stopped in the middle of a frame.")
                frame = np.frombuffer(chunk, dtype=np.uint8).reshape(720, 1280, 3)
                sample_time = frame_number
                if fingerprint_interval and sample_time % fingerprint_interval == 0:
                    fingerprints.append({"time": sample_time, "hash": frame_hash(frame)})
                if sample_time < INITIAL_DENSE_SAMPLE_SECONDS or sample_time % 2 == 0:
                    sample = reader.read(frame, sample_time)
                    detector.observe(sample)
                    diagnostic_candidate = (sample.round is None) != (sample.timer is None)
                    periodic_candidate = sample_time in {5, 305, 605, 905}
                    if diagnostic_count < 6 and (diagnostic_candidate or periodic_candidate):
                        diagnostic_directory.mkdir(exist_ok=True)
                        diagnostic = frame[:int(frame.shape[0] * .2)]
                        if cv2.imwrite(str(diagnostic_directory / f"hud-{sample_time:05d}.jpg"), diagnostic):
                            diagnostic_count += 1
                frame_number += 1
                with LOCK:
                    job["progress"] = min(98, 35 + int(frame_number / duration * 63))
            return_code = process.wait()
            stream_return_code = stream.wait() if stream else 0
        if return_code:
            message = error_path.read_text(errors="replace")[-400:] or stream_error_path.read_text(errors="replace")[-400:]
            raise ValueError("FFmpeg could not finish reading the video: " + message)
        if stream_return_code:
            raise ValueError("The video service stream ended with an error: " + stream_error_path.read_text(errors="replace")[-400:])
        detector.finalize()
        if not detector.rounds:
            raise ValueError("No reliable round starts were found. This version needs the VCT top-centre ROUND label and timer; a different layout may need detector changes.")
        update(identifier, status="ready", message="Index ready · review accuracy before relying on it", progress=100,
               rounds=detector.rounds, warnings=detector.warnings,
               source=str(source), duration=duration, fingerprints=fingerprints)
    except InterruptedError as error:
        update(identifier, status="cancelled", message=str(error), progress=0)
    except Exception as error:
        if CANCEL.is_set():
            update(identifier, status="cancelled", message="Indexing cancelled. Your original video has not been changed.", progress=0)
        else:
            update(identifier, status="error", message=str(error), progress=0)
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            if process.stdout:
                process.stdout.close()
        if stream is not None:
            if stream.poll() is None:
                stream.terminate()
                try:
                    stream.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    stream.kill()
            if stream.stdout and not stream.stdout.closed:
                stream.stdout.close()
        with LOCK:
            ACTIVE = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{PORT}"

    def do_GET(self):
        if not self.valid_host():
            self.respond(403, {"error": "Open the app at 127.0.0.1, not through another hostname."})
            return
        path = urlparse(self.path).path
        if path == "/api/state":
            with LOCK:
                jobs = [{key: value for key, value in job.items() if key not in {"source", "duration", "fingerprints"}} for job in JOBS.values()]
                self.respond(200, {"token": TOKEN, "active": ACTIVE, "jobs": jobs, "workspace": str(ROOT)})
            return
        match = re.fullmatch(r"/api/export/([a-f0-9]{32})", path)
        if match:
            with LOCK:
                job = JOBS.get(match[1])
                if not job or job["status"] != "ready":
                    self.respond(404, {"error": "This index is not ready."})
                else:
                    self.respond(200, export(job))
            return
        files = {"/": ("web/index.html", "text/html"), "/app.js": ("web/app.js", "text/javascript"),
                 "/style.css": ("web/style.css", "text/css")}
        if path not in files:
            self.respond(404, {"error": "Not found"})
            return
        name, content_type = files[path]
        body = (ROOT / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global ACTIVE
        if not self.valid_host() or self.headers.get("X-VODLOCK-Token") != TOKEN:
            self.respond(403, {"error": "Reload Round Studio before making changes."})
            return
        origin = self.headers.get("Origin")
        if origin and origin != f"http://127.0.0.1:{PORT}":
            self.respond(403, {"error": "Requests from other websites are not allowed."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 200000:
                raise ValueError("Invalid request size.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("The request must be a JSON object.")
            path = urlparse(self.path).path
            with LOCK:
                if path == "/api/cancel":
                    CANCEL.set()
                    self.respond(200, {"ok": True})
                    return
                if path == "/api/shutdown":
                    CANCEL.set()
                    self.respond(200, {"ok": True})
                    self.server.shutdown()
                    return
                if path == "/api/jobs":
                    if ACTIVE:
                        self.respond(409, {"error": "One VOD is already being processed. Cancel it or wait until it finishes."})
                        return
                    kind = body.get("kind")
                    identifier = uuid.uuid4().hex
                    job = {"id": identifier, "label": str(body.get("label", "")).strip()[:100] or "Untitled VOD",
                           "kind": kind, "status": "queued", "progress": 0, "message": "Preparing the indexer",
                           "rounds": [], "warnings": [], "created": time.time()}
                    if kind == "youtube":
                        job["videoId"] = video_id(str(body.get("source", "")))
                    elif kind == "twitch":
                        job["twitchVideoId"] = twitch_video_id(str(body.get("source", "")))
                    elif kind == "local":
                        source = Path(str(body.get("source", "")).strip().strip('"'))
                        if not source.is_absolute() or not source.is_file() or source.suffix.lower() not in {".mp4", ".mkv", ".webm", ".mov"}:
                            raise ValueError("Enter the full path to an existing MP4, MKV, WebM or MOV file on this PC.")
                        job["source"] = str(source.resolve())
                        linked = str(body.get("youtube", "")).strip()
                        job["videoId"] = video_id(linked) if linked else ""
                    else:
                        raise ValueError("Choose a YouTube link, Twitch VOD link, or local recording.")
                    JOBS[identifier] = job
                    save(job)
                    ACTIVE = identifier
                    CANCEL.clear()
                    threading.Thread(target=index_job, args=(identifier,)).start()
                    self.respond(201, {"id": identifier})
                    return
                match = re.fullmatch(r"/api/review/([a-f0-9]{32})", path)
                if match:
                    job = JOBS.get(match[1])
                    if not job or job["status"] != "ready":
                        raise ValueError("Only a finished index can be reviewed.")
                    action = body.get("action", "edit")
                    if action not in {"edit", "add", "exclude", "include"}:
                        raise ValueError("Choose a supported review action.")
                    rounds = [dict(entry) for entry in job["rounds"]]
                    if action == "add":
                        map_number = body.get("map")
                        number = body.get("round")
                        if type(map_number) is not int or not 1 <= map_number <= 99 or type(number) is not int or not 1 <= number <= 100:
                            raise ValueError("Enter a map from 1 to 99 and round from 1 to 100.")
                        if any(entry["map"] == map_number and entry["round"] == number for entry in rounds):
                            raise ValueError("This round already exists. Edit or re-include its existing entry.")
                        if len(rounds) >= 5000:
                            raise ValueError("The index already contains 5000 rounds.")
                        start = body.get("start")
                        if type(start) not in {int, float} or not math.isfinite(start):
                            raise ValueError("Enter a timestamp inside the recording.")
                        start = round(start, 2)
                        if not 0 <= start < job["duration"] or any(entry["start"] == start for entry in rounds):
                            raise ValueError("Enter a unique timestamp inside the recording.")
                        before = [entry for entry in rounds if entry["start"] < start]
                        after = [entry for entry in rounds if entry["start"] > start]
                        identity = (map_number, number)
                        if before and (before[-1]["map"], before[-1]["round"]) >= identity or after and (after[0]["map"], after[0]["round"]) <= identity:
                            raise ValueError("Map and round labels must follow timestamp order.")
                        rounds.append({"map": map_number, "round": number, "start": start, "verified": True})
                        rounds.sort(key=lambda entry: entry["start"])
                    else:
                        index = body.get("index")
                        if type(index) is not int or not 0 <= index < len(rounds):
                            raise ValueError("Choose an existing round.")
                        if body.get("map") != rounds[index]["map"] or body.get("round") != rounds[index]["round"]:
                            raise ValueError("The index changed. Close and reopen review before saving.")
                        if action in {"exclude", "include"}:
                            if action == "exclude" and not any(not entry.get("excluded") for position, entry in enumerate(rounds) if position != index):
                                raise ValueError("Keep at least one round in the index.")
                            rounds[index]["excluded"] = action == "exclude"
                        else:
                            start = body.get("start")
                            if type(start) not in {int, float} or not math.isfinite(start):
                                raise ValueError("Enter a timestamp inside the recording.")
                            start = round(start, 2)
                            if not 0 <= start < job["duration"]:
                                raise ValueError("Enter a timestamp inside the recording.")
                            if index > 0 and start <= rounds[index - 1]["start"] or index + 1 < len(rounds) and start >= rounds[index + 1]["start"]:
                                raise ValueError("Round timestamps must stay in chronological order.")
                            rounds[index].update(start=start, verified=True)
                    included = [entry for entry in rounds if not entry.get("excluded")]
                    warnings = []
                    if included[0]["round"] != 1:
                        warnings.append("The first included round is not round 1. Check the beginning of this recording.")
                    for previous, entry in zip(included, included[1:]):
                        if entry["map"] == previous["map"] and entry["round"] != previous["round"] + 1:
                            warnings.append(f"Map {entry['map']}: check the gap before round {entry['round']}.")
                    candidate = {**job, "rounds": rounds, "warnings": warnings}
                    save(candidate)
                    job.update(candidate)
                    self.respond(200, {"ok": True})
                    return
                self.respond(404, {"error": "Not found"})
        except (ValueError, TypeError, KeyError) as error:
            self.respond(400, {"error": str(error)})
        except OSError as error:
            self.respond(500, {"error": "Could not save the local index: " + str(error)})


def main():
    DATA.mkdir(exist_ok=True)
    for path in DATA.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(job, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(job.get("id", ""))) or path.stem != job["id"]:
                raise ValueError("Invalid saved job identifier.")
            if job.get("status") in {"queued", "downloading", "analyzing"}:
                job.update(status="error", progress=0, message="Processing was interrupted. Add this VOD again to retry.")
            JOBS[job["id"]] = job
        except (ValueError, KeyError, OSError) as error:
            print(f"Could not load saved index {path.name}: {error}", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"VODLOCK Round Studio is ready at http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        CANCEL.set()
    finally:
        CANCEL.set()
        server.server_close()


if __name__ == "__main__":
    main()
