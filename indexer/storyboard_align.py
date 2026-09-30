import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

import cv2
import imageio_ffmpeg
import numpy as np


ALIGNER_VERSION = "storyboard-v1"


def frame_hash(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    values = cv2.dct(np.float32(gray))[:8, :8]
    bits = values > np.median(values[1:])
    return np.packbits(bits.reshape(-1)).tobytes().hex()


def split_sheet(image, rows, columns, start, duration, interval):
    height, width = image.shape[:2]
    tile_height = height // rows
    tile_width = width // columns
    frames = []
    for index in range(rows * columns):
        time = start + index * interval
        if time >= start + duration - interval / 3:
            break
        row, column = divmod(index, columns)
        tile = image[row * tile_height:(row + 1) * tile_height,
                     column * tile_width:(column + 1) * tile_width]
        frames.append({"time": round(time, 3), "hash": frame_hash(tile)})
    return frames


def extract_storyboard(url, provider, yt_dlp, requester=None):
    options = {"quiet": True, "no_warnings": True, "format": "sb0"}
    node = shutil.which("node")
    if node:
        options["js_runtimes"] = {"node": {"path": node}}
    if provider == "youtube":
        client = "web" if os.environ.get("VODLOCK_YOUTUBE_POT") == "1" else "web_embedded"
        options["extractor_args"] = {"youtube": {"player_client": [client]}}
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
    formats = [item for item in info.get("formats", [])
               if str(item.get("format_id", "")).startswith("sb") and item.get("fragments")]
    if not formats:
        raise ValueError("No timeline storyboard is available for this video")
    storyboard = max(formats, key=lambda item: item.get("width", 0) * item.get("height", 0))
    rows = int(storyboard["rows"])
    columns = int(storyboard["columns"])
    fragments = storyboard["fragments"]
    full_durations = [float(item["duration"]) for item in fragments[:-1] if item.get("duration")]
    if not full_durations:
        full_durations = [float(fragments[0]["duration"])]
    interval = float(np.median(full_durations)) / (rows * columns)
    headers = storyboard.get("http_headers") or info.get("http_headers") or {}
    fetch = requester or (lambda target: urlopen(Request(target, headers=headers), timeout=30).read())
    frames = []
    position = 0.0
    for fragment in fragments:
        data = fetch(fragment["url"])
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("A storyboard image could not be decoded")
        duration = float(fragment.get("duration") or rows * columns * interval)
        frames.extend(split_sheet(image, rows, columns, position, duration, interval))
        position += duration
    return {"version": 1, "provider": provider, "sourceId": str(info["id"]).removeprefix("v"),
            "duration": round(float(info.get("duration") or position), 3),
            "interval": round(interval, 6), "frames": frames}


def fingerprint_video(path, source_id, interval=10):
    path = Path(path)
    capture = cv2.VideoCapture(str(path))
    duration = capture.get(cv2.CAP_PROP_FRAME_COUNT) / max(1, capture.get(cv2.CAP_PROP_FPS))
    opened = capture.isOpened()
    capture.release()
    if not opened or not np.isfinite(duration) or duration <= 0:
        raise ValueError("Could not read the fingerprint source video")
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
               "-an", "-vf", f"fps=fps=1/{interval}:start_time=0:round=up,scale=320:180",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    frames = []
    frame_size = 320 * 180 * 3
    try:
        while True:
            data = process.stdout.read(frame_size)
            if not data:
                break
            if len(data) != frame_size:
                raise ValueError("Video fingerprinting stopped in the middle of a frame")
            image = np.frombuffer(data, dtype=np.uint8).reshape(180, 320, 3)
            frames.append({"time": len(frames) * interval, "hash": frame_hash(image)})
        if process.wait():
            raise ValueError("FFmpeg could not fingerprint the source video")
    finally:
        if process.poll() is None:
            process.terminate()
        if process.stdout:
            process.stdout.close()
    return {"version": 1, "provider": "twitch", "sourceId": str(source_id),
            "duration": round(float(duration), 3), "interval": interval, "frames": frames}


def hamming(left, right):
    return sum((first ^ second).bit_count() for first, second in zip(bytes.fromhex(left), bytes.fromhex(right)))


def align_storyboards(reference, target, maximum_distance=12, require_target_coverage=True):
    if len(reference.get("frames", [])) < 30 or len(target.get("frames", [])) < 30:
        raise ValueError("Not enough storyboard frames for a verified alignment")
    reference_frames = reference["frames"]
    target_frames = target["frames"]
    matches = []
    for target_frame in target_frames:
        distances = [(hamming(target_frame["hash"], item["hash"]), item["time"])
                     for item in reference_frames]
        distances.sort(key=lambda item: item[0])
        if distances[0][0] <= maximum_distance and (len(distances) == 1 or distances[1][0] - distances[0][0] >= 2):
            matches.append({"target": target_frame["time"], "reference": distances[0][1],
                            "distance": distances[0][0], "offset": distances[0][1] - target_frame["time"]})
    if len(matches) < 12:
        raise ValueError("The videos do not contain enough matching visual anchors")
    tolerance = (reference.get("interval", 10) + target.get("interval", 10)) / 2 + 1
    candidates = []
    for match in matches:
        cluster = [item for item in matches if abs(item["offset"] - match["offset"]) <= tolerance]
        if len(cluster) >= 5 and max(item["target"] for item in cluster) - min(item["target"] for item in cluster) >= 60:
            candidates.append(cluster)
    distinct = []
    for cluster in sorted(candidates, key=len, reverse=True):
        offset = float(np.median([item["offset"] for item in cluster]))
        if not any(abs(offset - item["offset"]) <= tolerance * 2 for item in distinct):
            distinct.append({"offset": offset, "matches": cluster})
    distinct.sort(key=lambda item: min(match["target"] for match in item["matches"]))
    segments = []
    for item in distinct:
        cluster = item["matches"]
        offset = item["offset"]
        verified = [match for match in cluster if abs(match["offset"] - offset) <= tolerance]
        start = min(match["target"] for match in verified)
        end = max(match["target"] for match in verified)
        if segments and (start <= segments[-1]["anchorEnd"] or
                         min(match["reference"] for match in verified) <= segments[-1]["referenceEnd"] or
                         offset < segments[-1]["offset"] - tolerance):
            continue
        segments.append({"offset": offset, "anchors": len(verified), "anchorStart": start,
                         "anchorEnd": end, "referenceEnd": max(match["reference"] for match in verified),
                         "matches": verified})
    if not segments:
        raise ValueError("The videos do not have a consistent storyboard alignment")
    coefficients = None
    for attempt in range(2):
        matrix = []
        values = []
        for group, segment in enumerate(segments):
            for match in segment["matches"]:
                matrix.append([match["target"], *[1 if index == group else 0 for index in range(len(segments))]])
                values.append(match["reference"])
        coefficients = np.linalg.lstsq(np.asarray(matrix), np.asarray(values), rcond=None)[0]
        if attempt == 0:
            for index, segment in enumerate(segments):
                segment["matches"] = [match for match in segment["matches"]
                                      if abs(match["reference"] - (coefficients[0] * match["target"] + coefficients[index + 1])) <= 3]
                if len(segment["matches"]) < 5:
                    raise ValueError("The videos do not have enough precise visual anchors")
    scale = float(coefficients[0])
    if not 0.99 <= scale <= 1.01:
        raise ValueError("The videos do not share a stable timeline scale")
    for index, segment in enumerate(segments):
        segment["offset"] = float(coefficients[index + 1])
        segment["anchors"] = len(segment["matches"])
        residuals = [abs(match["reference"] - (scale * match["target"] + segment["offset"]))
                     for match in segment["matches"]]
        segment["medianDistance"] = round(float(np.median([match["distance"] for match in segment["matches"]])), 3)
        segment["maximumResidual"] = round(max(residuals), 3)
        if segment["maximumResidual"] > tolerance:
            raise ValueError("The videos do not have a precise storyboard alignment")
    for index, segment in enumerate(segments):
        segment["targetStart"] = 0 if index == 0 else round(scale * (segments[index - 1]["anchorEnd"] + segment["anchorStart"]) / 2, 3)
        segment["targetEnd"] = round(float(target["duration"]), 3) if index + 1 == len(segments) else round(scale * (segment["anchorEnd"] + segments[index + 1]["anchorStart"]) / 2, 3)
    verified = [match for segment in segments for match in segment["matches"]]
    duration = float(target["duration"] if require_target_coverage else reference["duration"])
    coverage_key = "target" if require_target_coverage else "reference"
    thirds = [sum(1 for item in verified if lower <= item[coverage_key] < upper)
              for lower, upper in [(0, duration / 3), (duration / 3, duration * 2 / 3), (duration * 2 / 3, duration + 1)]]
    if len(verified) < 12 or min(thirds) < 2:
        raise ValueError("Visual anchors do not cover the beginning, middle, and end of the match")
    public_segments = [{key: round(value, 3) if key == "offset" else value for key, value in segment.items()
                        if key not in {"anchorStart", "anchorEnd", "referenceEnd", "matches"}}
                       for segment in segments]
    return {"version": ALIGNER_VERSION, "offset": public_segments[0]["offset"], "segments": public_segments,
            "timelineScale": round(scale, 8), "anchors": len(verified),
            "coverage": thirds, "medianDistance": round(float(np.median([item["distance"] for item in verified])), 3),
            "maximumResidual": max(segment["maximumResidual"] for segment in public_segments)}


def translate_index(index, target, alignment):
    duration = float(target["duration"])
    segments = alignment.get("segments") or [{"offset": alignment["offset"], "targetStart": 0, "targetEnd": duration}]
    selected = []
    for item in index["rounds"]:
        for segment in segments:
            start = (float(item["start"]) - float(segment["offset"])) / float(alignment.get("timelineScale", 1))
            if max(0, float(segment["targetStart"])) <= start < min(duration, float(segment["targetEnd"])):
                selected.append({**item, "translatedStart": start})
                break
    if not selected:
        raise ValueError("No indexed rounds fall inside the aligned YouTube match")
    first_map = int(selected[0]["map"])
    translated = [{"map": int(item["map"]) - first_map + 1, "round": int(item["round"]),
                   "start": round(float(item["translatedStart"]), 2)} for item in selected]
    if translated[0]["map"] != 1 or translated[0]["round"] != 1:
        raise ValueError("The aligned match does not begin at map 1 round 1")
    previous = None
    for item in translated:
        if item["map"] < 1 or item["start"] < 0:
            raise ValueError("The translated round index is invalid")
        if previous:
            same_map = item["map"] == previous["map"] and item["round"] == previous["round"] + 1
            next_map = item["map"] == previous["map"] + 1 and item["round"] == 1 and previous["round"] >= 12
            if not same_map and not next_map:
                raise ValueError("The translated round sequence contains a gap")
        previous = item
    if len(translated) < 13:
        raise ValueError("The aligned match contains fewer than 13 rounds")
    return translated


def save_storyboard(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def load_storyboard(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
