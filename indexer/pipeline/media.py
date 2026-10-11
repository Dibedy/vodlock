import math
import logging
import re
import subprocess
import tempfile
import time
import threading
import csv
from collections import deque
from datetime import timedelta
from fractions import Fraction
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import imageio_ffmpeg
import cv2
import numpy as np

from detector import HudReader, Observation
from server import compact_analysis_filter
from storyboard_align import frame_hashes

from .errors import Unsupported, WaitingSource, WaitingWork
from .detection import CandidateDetector
from .schedule import fetch_bytes
from .youtube import timestamp as parse_timestamp


LOG = logging.getLogger(__name__)


def parse_playlist(text, url):
    if not text.startswith("#EXTM3U"):
        raise WaitingSource("The media URL did not return an HLS playlist")
    sequence = 0
    duration = None
    wall = None
    init = None
    group = 0
    segments = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            wall = parse_timestamp(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE"):
            group += 1
            wall = None
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",")[0])
            if not math.isfinite(duration) or not 0 < duration <= 60:
                raise Unsupported("HLS segment duration must be between 0 and 60 seconds")
        elif line.startswith("#EXT-X-MAP:"):
            found = re.search(r'URI="([^"]+)"', line)
            if not found or "BYTERANGE=" in line:
                raise Unsupported("HLS initialization byte ranges require a source adapter")
            init = urljoin(url, found[1])
        elif line.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in line:
            raise Unsupported("Encrypted HLS is unsupported")
        elif line.startswith("#EXT-X-BYTERANGE:"):
            raise Unsupported("HLS media byte ranges are unsupported")
        elif line and not line.startswith("#") and duration is not None:
            segments.append(
                {
                    "sequence": sequence,
                    "duration": duration,
                    "wall_time": wall,
                    "url": urljoin(url, line),
                    "init": init,
                    "group": group,
                }
            )
            if wall:
                wall += timedelta(seconds=duration)
            sequence += 1
            duration = None
    for index in range(len(segments) - 2, -1, -1):
        current, following = segments[index : index + 2]
        if current["wall_time"] is None and following["wall_time"] and current["group"] == following["group"]:
            current["wall_time"] = following["wall_time"] - timedelta(seconds=current["duration"])
    if not segments:
        raise WaitingSource("No media segments are available in the HLS playlist")
    return segments


def decode_frames(media, duration, start=0, headers=None, compact=True, interval=1, accurate=False, absolute=False):
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-rw_timeout",
        "20000000",
    ]
    if headers:
        safe = {
            str(key): str(value) for key, value in headers.items() if not re.search(r"[\r\n]", str(key) + str(value))
        }
        command.extend(["-headers", "".join(f"{key}: {value}\r\n" for key, value in safe.items())])
    if start and not accurate:
        if absolute:
            command.extend(["-seek_timestamp", "1"])
        command.extend(["-ss", str(start)])
    command.extend(["-i", str(media)])
    if start and accurate:
        command.extend(["-ss", str(start)])
    command.extend(["-t", str(duration), "-an"])
    if interval != 1:
        video_filter = f"setpts=PTS-STARTPTS,fps=fps=1/{interval}:start_time=0:round=up,scale=320:180"
        height, width = 180, 320
    elif compact:
        video_filter = compact_analysis_filter()
        height, width = 324, 1280
    else:
        video_filter = "setpts=PTS-STARTPTS,fps=fps=1:start_time=0:round=up,scale=1280:720"
        height, width = 720, 1280
    if accurate:
        video_filter = f"fps=fps=1/{interval}:start_time={start}:round=up,scale={width}:{height}"
    command.extend(["-vf", video_filter, "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"])
    size = height * width * 3
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=errors, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        assert process.stdout is not None
        stopped = threading.Event()
        stalled = threading.Event()
        reading: list[float | None] = [None]

        def watchdog():
            while not stopped.wait(1):
                begun = reading[0]
                if begun is not None and monotonic() - begun > 45:
                    stalled.set()
                    process.kill()
                    return

        monitor = threading.Thread(target=watchdog, daemon=True)
        monitor.start()
        try:
            number = 0
            while True:
                buffer = bytearray()
                while len(buffer) < size:
                    reading[0] = monotonic()
                    try:
                        chunk = process.stdout.read(size - len(buffer))
                    finally:
                        reading[0] = None
                    if stalled.is_set():
                        raise WaitingSource("FFmpeg decoder stalled for 45 seconds; retrying from the durable checkpoint")
                    if not chunk:
                        break
                    buffer.extend(chunk)
                if not buffer:
                    break
                if len(buffer) != size:
                    raise WaitingSource("FFmpeg stopped inside a frame; refresh the source URL")
                offset = number * interval
                if offset < duration:
                    yield offset, np.frombuffer(buffer, dtype=np.uint8).reshape(height, width, 3)
                number += 1
            if process.wait(timeout=30):
                errors.seek(0)
                evidence = re.sub(r"https?://[^\s\"']+", "[media URL]", errors.read().decode(errors="replace"))
                raise WaitingSource("FFmpeg media read failed: " + evidence[-1000:])
        finally:
            stopped.set()
            monitor.join(timeout=2)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()


def recognized_teams(lines, aliases):
    text = (
        " " + " ".join(re.sub(r"[^A-Z0-9]+", " ", item[0].upper()).strip() for item in lines if item[1] >= 0.85) + " "
    )
    found = []
    for team, names in aliases.items():
        if any(" " + re.sub(r"[^A-Z0-9]+", " ", name.upper()).strip() + " " in text for name in names):
            found.append(team)
    return sorted(found) if len(found) == 2 else None


class MediaAnalysis:
    def __init__(self, requester=fetch_bytes, decoder=decode_frames, reader=None):
        self.requester = requester
        self.decoder = decoder
        self.reader = reader
        self.stream_cache: dict[tuple, dict] = {}
        self.stream_lock = threading.Lock()
        self.ocr_seconds = 0.0
        self.ocr_calls = 0
        self.video_cache: dict[tuple, dict] = {}
        self.video_lock = threading.Lock()

    def close(self):
        with self.stream_lock:
            streams = list(self.stream_cache.values())
            self.stream_cache.clear()
        for stream in streams:
            if hasattr(stream["frames"], "close"):
                stream["frames"].close()
        with self.video_lock:
            for value in self.video_cache.values():
                if value["references"]:
                    raise RuntimeError("Cannot close a video cache with active readers")
                value["path"].unlink(missing_ok=True)
            self.video_cache.clear()

    def download_clip(self, remote, lower, upper, path):
        started = monotonic()
        duration = float(remote["duration"])
        beginning, ending = max(0, lower - 5), min(duration, upper + 5)
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-rw_timeout", "20000000"]
        headers = {str(key): str(value) for key, value in (remote.get("headers") or {}).items()
                   if not re.search(r"[\r\n]", str(key) + str(value))}
        if headers:
            command.extend(["-headers", "".join(f"{key}: {value}\r\n" for key, value in headers.items())])
        command.extend(["-ss", str(beginning), "-t", str(ending - beginning), "-copyts", "-start_at_zero",
                        "-i", str(remote["url"]), "-an", "-map", "0:v:0", "-c:v", "copy",
                        "-avoid_negative_ts", "disabled", "-fs", str(128 * 1024 * 1024), "-f", "nut", "-y", str(path)])
        try:
            result = subprocess.run(command, capture_output=True, timeout=150,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                evidence = re.sub(r"https?://[^\s\"']+", "[media URL]", result.stderr.decode(errors="replace"))
                raise WaitingSource("Archive clip download failed: " + evidence[-1000:])
            if path.stat().st_size > 128 * 1024 * 1024:
                raise WaitingSource("Archive clip exceeded its cache size limit")
            result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin",
                                     "-copyts", "-i", str(path), "-map", "0:v:0", "-c:v", "copy", "-f", "framecrc", "-"],
                                    capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as error:
            raise WaitingSource("Archive clip download or timestamp verification timed out") from error
        text = result.stdout.decode(errors="replace")
        base = re.search(r"^#tb 0: (\d+)/(\d+)$", text, re.MULTILINE)
        if result.returncode or not base:
            raise WaitingSource("Archive clip has no verified packet timebase")
        try:
            scale = Fraction(int(base[1]), int(base[2]))
            packets = sorted((int(row[2]) * scale, int(row[3]) * scale)
                             for row in csv.reader(line for line in text.splitlines() if line and not line.startswith("#")))
        except (ValueError, IndexError, ZeroDivisionError) as error:
            raise WaitingSource("Archive clip packet timestamps are invalid") from error
        if (not packets or packets[0][0] > lower + .1 or max(position + length for position, length in packets) < upper - .001
                or any(right[0] - left[0] - left[1] > .1 for left, right in zip(packets, packets[1:]))):
            raise WaitingSource("Archive clip packet timestamps do not cover its requested range continuously")
        LOG.info("archive_clip_downloaded start=%.3f end=%.3f bytes=%s elapsed_seconds=%.3f",
                 lower, upper, path.stat().st_size, monotonic() - started)

    def cached_archive_frames(self, remote, duration, start=0, compact=False, interval=1):
        position = start
        while position < start + duration:
            lower = position
            upper = min(position + 120, float(remote["duration"]), start + duration)
            identity = (tuple(remote["cache_identity"]), tuple(remote.get("cache_revision", ())))
            key = (*identity, lower, upper)
            owner = False
            with self.video_lock:
                now = monotonic()
                for prior, value in list(self.video_cache.items()):
                    if not value["references"] and (value.get("error") or value["expires"] <= now):
                        value["path"].unlink(missing_ok=True)
                        del self.video_cache[prior]
                matching = [(prior, value) for prior, value in self.video_cache.items()
                            if prior[:2] == identity and value["start"] <= position
                            and value["end"] >= min(position + interval, start + duration) and not value.get("error")]
                if matching:
                    key, entry = max(matching, key=lambda item: item[1]["end"])
                    upper = min(upper, entry["end"])
                else:
                    entry = None
                if entry is None:
                    while len(self.video_cache) >= 4:
                        idle = [(value["used"], prior) for prior, value in self.video_cache.items() if not value["references"]]
                        if not idle:
                            raise WaitingWork("Archive video cache is busy with active readers")
                        prior = min(idle, key=lambda value: value[0])[1]
                        self.video_cache.pop(prior)["path"].unlink(missing_ok=True)
                    with tempfile.NamedTemporaryFile(prefix="spoilless-archive-", suffix=".nut", delete=False) as output:
                        path = Path(output.name)
                    entry = {"path": path, "ready": threading.Event(), "references": 0, "expires": now + 300,
                             "used": now, "start": lower, "end": upper}
                    self.video_cache[key] = entry
                    owner = True
                entry["references"] += 1
                entry["used"] = now
            try:
                if owner:
                    self.download_clip(remote, lower, upper, entry["path"])
                    entry["ready"].set()
                elif not entry["ready"].wait(180):
                    raise WaitingSource("Shared archive clip download did not finish")
                if entry.get("error"):
                    raise entry["error"]
                count = min(math.ceil((upper - position) / interval), math.ceil((start + duration - position) / interval))
                end = min(start + duration, position + count * interval)
                frames = self.decoder(entry["path"], end - position, start=position, compact=compact, interval=interval, absolute=True)
                decoded = 0
                try:
                    for offset, frame in frames:
                        if abs(offset - decoded * interval) > .001 or decoded >= count:
                            raise WaitingSource("Cached archive decoder returned an unexpected frame timestamp")
                        yield position - start + offset, frame
                        decoded += 1
                    if decoded != count:
                        raise WaitingSource("Cached archive decoder ended before its requested range")
                finally:
                    if hasattr(frames, "close"):
                        frames.close()
                position += count * interval
            except Exception as error:
                with self.video_lock:
                    entry["error"] = error
                if owner:
                    entry["ready"].set()
                raise
            finally:
                with self.video_lock:
                    entry["references"] -= 1
                    if entry.get("error") and not entry["references"]:
                        entry["path"].unlink(missing_ok=True)
                        if self.video_cache.get(key) is entry:
                            del self.video_cache[key]

    def archive_frames(self, remote, start, end, compact=False, interval=1, stream_id=None):
        key = (remote["url"], tuple(sorted((remote.get("headers") or {}).items())), compact, interval, stream_id)
        now = time.monotonic()
        with self.stream_lock:
            expired = [self.stream_cache.pop(key) for key, value in list(self.stream_cache.items()) if value["expires"] <= now]
            stream = self.stream_cache.pop(key, None)
        for value in expired:
            if hasattr(value["frames"], "close"):
                value["frames"].close()
        if stream and (abs(stream["next"] - start) > .001 or stream["end"] < end):
            if hasattr(stream["frames"], "close"):
                stream["frames"].close()
            stream = None
        if stream is None:
            stop = min(end if remote.get("cache_identity") else max(start + 600, end), float(remote.get("duration") or end))
            frames = (self.cached_archive_frames(remote, stop - start, start=start, compact=compact, interval=interval)
                      if remote.get("cache_identity") else self.decoder(remote["url"], stop - start, start=start,
                                                                        headers=remote.get("headers"), compact=compact, interval=interval))
            stream = {"base": start, "next": start, "end": stop,
                      "frames": iter(frames)}
        complete = False
        try:
            while stream["next"] < end:
                try:
                    offset, frame = next(stream["frames"])
                except StopIteration:
                    if stream["next"] < min(end, stream["end"]) - .001:
                        raise WaitingSource("Archive decoder ended before its requested range")
                    break
                timestamp = stream["base"] + offset
                stream["next"] = timestamp + interval
                yield timestamp, frame
            if stream["next"] >= stream["end"]:
                try:
                    next(stream["frames"])
                except StopIteration:
                    pass
                else:
                    raise WaitingSource("Archive decoder exceeded its bounded range")
            complete = stream["next"] >= end and stream["next"] < stream["end"]
        finally:
            if complete:
                stream["expires"] = time.monotonic() + 120
                with self.stream_lock:
                    evicted = []
                    while len(self.stream_cache) >= 2:
                        evicted.append(self.stream_cache.pop(next(iter(self.stream_cache))))
                    if key in self.stream_cache:
                        evicted.append(self.stream_cache.pop(key))
                    self.stream_cache[key] = stream
                for value in evicted:
                    if hasattr(value["frames"], "close"):
                        value["frames"].close()
            else:
                if hasattr(stream["frames"], "close"):
                    stream["frames"].close()

    def manifest(self, remote, start_sequence=None):
        url = remote["url"]
        for _ in range(4):
            if start_sequence is not None:
                parts = urlsplit(url)
                query = [(key, value) for key, value in parse_qsl(parts.query) if key != "start_seq"]
                url = urlunsplit(parts._replace(query=urlencode([*query, ("start_seq", str(start_sequence))])))
            text = self.requester(url, remote.get("headers")).decode("utf-8")
            if "#EXT-X-STREAM-INF:" not in text:
                return parse_playlist(text, url)
            variants = []
            attributes = None
            for line in text.splitlines():
                if line.startswith("#EXT-X-STREAM-INF:"):
                    attributes = line
                elif attributes and line and not line.startswith("#"):
                    resolution = re.search(r"RESOLUTION=\d+x(\d+)", attributes)
                    height = int(resolution[1]) if resolution else 0
                    if 0 < height <= 720:
                        variants.append((height, urljoin(url, line)))
                    attributes = None
            if not variants:
                raise WaitingSource("HLS master playlist has no supported video variant")
            preferred = [item for item in variants if item[0] <= 540] or variants
            url = max(preferred)[1]
        raise Unsupported("HLS playlists exceed the nesting limit")

    def live_frames(self, remote, segment):
        with tempfile.TemporaryDirectory(prefix="spoilless-segment-") as directory:
            from pathlib import Path

            path = Path(directory) / "segment.bin"
            prefix = self.requester(segment["init"], remote.get("headers")) if segment.get("init") else b""
            media = self.requester(segment["url"], remote.get("headers"))
            if len(prefix) + len(media) > 64 * 1024 * 1024:
                raise Unsupported("A media segment exceeded the memory limit")
            path.write_bytes(prefix + media)
            yield from self.decoder(path, segment["duration"])

    def live_presentation_time(self, remote, segment):
        with tempfile.TemporaryDirectory(prefix="spoilless-clock-") as directory:
            from pathlib import Path

            path = Path(directory) / "segment.bin"
            prefix = self.requester(segment["init"], remote.get("headers")) if segment.get("init") else b""
            media = self.requester(segment["url"], remote.get("headers"))
            if len(prefix) + len(media) > 64 * 1024 * 1024:
                raise Unsupported("A media segment exceeded the memory limit")
            path.write_bytes(prefix + media)
            try:
                result = subprocess.run(
                    [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", "-copyts", "-i", str(path),
                     "-an", "-vf", "showinfo", "-frames:v", "1", "-f", "null", "-"],
                    capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as error:
                raise WaitingSource("YouTube DVR presentation-clock probe timed out") from error
            output = result.stderr.decode(errors="replace")
            found = re.search(r"n:\s*0\s+pts:\s*-?\d+\s+pts_time:([-+\d.eE]+)", output)
            if result.returncode or not found:
                raise WaitingSource("YouTube DVR returned no decodable presentation-clock anchor")
            value = float(found[1])
            if not math.isfinite(value) or value < 0:
                raise WaitingSource("YouTube DVR presentation-clock anchor is invalid")
            return value

    def intro_context(self, frame):
        if self.reader is None:
            self.reader = HudReader()
        lines = self.reader.read_lines(frame[:100, :320])
        text = " ".join(value.upper() for value, confidence in lines if confidence >= 0.85)
        countdown = re.search(r"\b([0-9]{2})\s*[:.]\s*([0-5][0-9])\s*[:.]\s*([0-5][0-9])\b", text)
        matchup = bool(re.search(r"(?:\bVS\b|[A-Z0-9]{2,}VS[A-Z0-9]{2,})", text))
        seconds = sum(int(value) * scale for value, scale in zip(countdown.groups(), (3600, 60, 1))) if countdown else 0
        return {"version": 1, "intro": bool(countdown and matchup), "countdown_seconds": seconds, "raw_lines": lines}

    def observation(self, frame, time, aliases=None, compact=True, dense=True, previous=None):
        started = monotonic()
        self.ocr_calls += 1
        if self.reader is None:
            self.reader = HudReader()
        sample = (
            self.reader.read(frame, time, compact=compact)
            if dense
            else self.reader.read_clock(frame, time, compact=compact)
        )
        if not dense and sample.timer is not None and 82 <= sample.timer <= 100:
            if previous:
                probe = self.reader.read_scoreboard(frame, time, compact=compact)
                if probe.replay or (probe.round == previous["round"] and probe.scores is not None
                                    and list(probe.scores) == list(previous.get("scores") or [])):
                    self.ocr_seconds += monotonic() - started
                    return probe, {"probe_only": True}
            sample = self.reader.read(frame, time, compact=compact)
        extra = {}
        if sample.round is not None and sample.timer is not None and 85 <= sample.timer <= 100:
            extra["intro_context"] = self.intro_context(frame)
            if extra["intro_context"]["intro"]:
                sample.replay = True
        if sample.round is not None and sample.timer is not None and sample.timer >= 85 and aliases:
            extra["teams"] = recognized_teams(self.reader.read_lines(frame[:100, :]), aliases)
        self.ocr_seconds += monotonic() - started
        return sample, extra

    def fingerprint(self, frame, compact=True):
        return frame_hashes(frame[144:324, :320] if compact else cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA))

    def scan_frames(self, frames, aliases=None, compact=False, detector=None, replay_frames=()):
        own_detector = detector is None
        detector = detector or CandidateDetector()
        state = detector.scan
        buffered: deque = deque()
        recent: deque = deque(maxlen=14)
        history: dict[str, list[float]] = {}
        last_hud = None
        for value in replay_frames:
            fingerprint = value.get("gameplayHash")
            if fingerprint:
                history.setdefault(fingerprint, []).append(value["time"])
        confirmed = (detector.previous or {}).get("start")
        for timestamp, frame in frames:
            buffered.append([timestamp, frame, None])
            if history and not compact:
                recent.append((timestamp, self.fingerprint(frame, compact=False)["gameplayHash"]))
            if (detector.previous or {}).get("start") != confirmed:
                confirmed = (detector.previous or {}).get("start")
                state["dense_until"] = -1
                state["mode"] = "gameplay"
            dense = timestamp <= state.get("dense_until", -1)
            changed_hud = False
            if state.get("mode") == "break" and isinstance(frame, np.ndarray):
                height, width = (720, 1280) if compact else frame.shape[:2]
                crop = frame[int(height * .026):int(height * .09), int(width * .44):int(width * .56)]
                gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                hud = cv2.resize(gray, (32, 16), interpolation=cv2.INTER_AREA)
                clock_visible = np.mean(gray < 70) >= .25 and .02 <= np.mean(gray > 180) <= .45
                changed_hud = bool(clock_visible and (last_hud is None or np.mean(cv2.absdiff(hud, last_hud)) >= 4))
                last_hud = hud
            if not dense and (changed_hud or timestamp >= state.get("next_probe", -1)):
                sample, extra = self.observation(frame, timestamp, aliases, compact=compact,
                                                 dense=False, previous=detector.previous)
                buffered[-1][2] = sample, extra
                signature = [sample.round, sample.timer, list(sample.scores or ())]
                frozen = signature == state.get("probe")
                state["probe"] = signature
                if sample.timer is None or sample.replay:
                    state.setdefault("quiet_since", timestamp)
                    if timestamp - state["quiet_since"] >= 30:
                        state["mode"] = "break"
                else:
                    state.pop("quiet_since", None)
                state["next_probe"] = timestamp + (6 if state.get("mode") == "break" else 4)
                if (not dense and not frozen and not extra.get("probe_only") and not sample.replay
                        and not sample.buy_phase and sample.timer is not None and 82 <= sample.timer <= 100):
                    state["dense_until"] = timestamp + 6
                    state["focus_start"] = timestamp - 6
                    state["mode"] = "checking"
            offsets: dict[int, set[str]] = {}
            if history and not compact:
                for position, fingerprint in recent:
                    for earlier in history.get(fingerprint, ()):
                        if position - earlier >= 30:
                            offsets.setdefault(round(position - earlier), set()).add(fingerprint)
            replay_offset = next((offset for offset, hashes in offsets.items() if len(hashes) >= 3), None)
            replay_times = [position for position, fingerprint in recent
                            if replay_offset is not None and any(abs(position - earlier - replay_offset) <= 1
                                                                 for earlier in history.get(fingerprint, ()))]
            while buffered and timestamp - buffered[0][0] >= 6:
                position, image, saved = buffered.popleft()
                if (state.get("focus_start", float("inf")) <= position <= state.get("dense_until", -1)
                        and (detector.previous or {}).get("start") == confirmed):
                    saved = saved or self.observation(image, position, aliases, compact=compact)
                sample, extra = saved or (empty_observation(position), {})
                if replay_times and min(replay_times) <= position <= max(replay_times):
                    sample.replay = True
                    extra = {**extra, "replay_history_offset": replay_offset}
                yield sample, extra, image
                if own_detector:
                    detector.observe(sample, extra=extra)
        while buffered:
            position, image, saved = buffered.popleft()
            if (state.get("focus_start", float("inf")) <= position <= state.get("dense_until", -1)
                    and (detector.previous or {}).get("start") == confirmed):
                saved = saved or self.observation(image, position, aliases, compact=compact)
            sample, extra = saved or (empty_observation(position), {})
            if replay_times and min(replay_times) <= position <= max(replay_times):
                sample.replay = True
                extra = {**extra, "replay_history_offset": replay_offset}
            yield sample, extra, image
            if own_detector:
                detector.observe(sample, extra=extra)

    def window(self, remote, start, end, aliases=None, compact=False, adaptive=False, detector=None, replay_frames=(), stream_id=None):
        started = time.monotonic()
        ocr_before, calls_before = self.ocr_seconds, self.ocr_calls
        decoded = analyzed = 0
        selected = remote
        if adaptive and remote.get("cache_identity"):
            identity = (tuple(remote["cache_identity"]), tuple(remote.get("cache_revision", ())))
            with self.video_lock:
                available = any(key[:2] == identity and value["ready"].is_set() and not value.get("error")
                                and value["expires"] > monotonic() and value["start"] <= start and value["end"] >= end
                                for key, value in self.video_cache.items())
            if not available:
                selected = {**remote, "cache_identity": None}
        frames = self.archive_frames(selected, start, end, compact=compact, stream_id=stream_id)
        analyzed_frames = self.scan_frames(frames, aliases, compact, detector, replay_frames) if adaptive else (
            (*self.observation(frame, timestamp, aliases, compact=compact), frame) for timestamp, frame in frames)
        try:
            for sample, extra, frame in analyzed_frames:
                decoded += 1
                analyzed += int(sample.timer is not None or sample.round is not None)
                yield sample, extra, frame
        finally:
            analyzed_frames.close()
            frames.close()
        LOG.info("video_window_analyzed start=%.3f end=%.3f decoded=%s analyzed=%s ocr_calls=%s ocr_seconds=%.3f elapsed_seconds=%.3f adaptive=%s mode=%s",
                 start, end, decoded, analyzed, self.ocr_calls - calls_before, self.ocr_seconds - ocr_before,
                 time.monotonic() - started, adaptive, detector.scan.get("mode", "gameplay") if detector else "gameplay")

    def archive_fingerprints(self, remote, start, end, interval=10):
        for timestamp, frame in self.archive_frames(remote, start, end, interval=interval):
            yield {"time": timestamp, **self.fingerprint(frame, compact=False)}

    def watchparty_window(self, remote, start, end):
        from pathlib import Path

        if "vod_segments" not in remote:
            remote["vod_segments"] = self.manifest(remote)
        position = 0.0
        group = None
        group_start = 0.0
        requested_group = None
        requested_group_start = 0.0
        for segment in remote["vod_segments"]:
            if group is not None and segment["group"] != group and start < position < end:
                for lower, upper in [(start, position), (position, end)]:
                    window = self.watchparty_window(remote, lower, upper)
                    try:
                        yield from window
                    finally:
                        window.close()
                return
            if segment["group"] != group:
                group_start = position
            if position <= start < position + segment["duration"]:
                requested_group = segment["group"]
                requested_group_start = group_start
            group = segment["group"]
            position += segment["duration"]
            if position >= end:
                break
        if end - start > 25:
            position = start
            while position < end:
                stop = min(position + 25, end)
                window = self.watchparty_window(remote, position, stop)
                try:
                    yield from window
                finally:
                    window.close()
                position = stop
            return
        position = 0.0
        selected = []
        base = None
        for segment in remote["vod_segments"]:
            following = position + segment["duration"]
            if position < end and following > max(requested_group_start, start - 20) and segment["group"] == requested_group:
                if base is None:
                    base = position
                selected.append(segment)
            position = following
            if position >= end:
                break
        if base is None:
            raise WaitingSource("Twitch VOD has no media segments in the requested scoreboard window")
        if len({segment["group"] for segment in selected}) != 1:
            raise WaitingSource("Twitch scoreboard window crosses a media discontinuity")
        with tempfile.TemporaryDirectory(prefix="spoilless-scoreboard-") as directory:
            path = Path(directory) / "window.ts"
            size = 0
            cache = remote.setdefault("vod_segment_cache", {})
            with path.open("wb") as output:
                if selected[0].get("init"):
                    data = self.requester(selected[0]["init"], remote.get("headers"))
                    size += len(data)
                    output.write(data)
                for segment in selected:
                    data = cache.get(segment["url"])
                    if data is None:
                        data = self.requester(segment["url"], remote.get("headers"))
                        while cache and (len(cache) >= 12 or sum(len(value) for value in cache.values()) + len(data) > 64 * 1024 * 1024):
                            del cache[next(iter(cache))]
                        if len(data) <= 64 * 1024 * 1024:
                            cache[segment["url"]] = data
                    size += len(data)
                    if size > 128 * 1024 * 1024:
                        raise WaitingSource("Twitch scoreboard chunk exceeded its bounded disk limit")
                    output.write(data)
            if self.reader is None:
                self.reader = HudReader()
            frames = self.decoder(path, end - start, start=start - base, compact=False, accurate=True)
            try:
                for offset, frame in frames:
                    yield self.reader.read_scoreboard(frame, start + offset), {}, frame
            finally:
                if hasattr(frames, "close"):
                    frames.close()


def empty_observation(time):
    return Observation(time, None, None)
