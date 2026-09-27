import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import imageio_ffmpeg
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))
from detector import HudReader, RoundDetector


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("output")
    arguments = parser.parse_args()
    reader = HudReader()
    detector = RoundDetector()
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", arguments.video,
               "-an", "-vf", "fps=fps=1/2:start_time=0:round=up,scale=1280:720", "-f", "rawvideo",
               "-pix_fmt", "bgr24", "pipe:1"]
    observations = []
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        frame_size = 1280 * 720 * 3
        while True:
            chunk = process.stdout.read(frame_size)
            if not chunk:
                break
            if len(chunk) != frame_size:
                raise ValueError("Incomplete decoded frame")
            frame = np.frombuffer(chunk, dtype=np.uint8).reshape(720, 1280, 3)
            sample = reader.read(frame, len(observations) * 2)
            observations.append(asdict(sample))
            detector.observe(sample)
            if len(observations) % 300 == 0:
                print(f"{sample.time / 60:.0f} minutes: {len(detector.rounds)} rounds", flush=True)
        if process.wait():
            raise ValueError("Video decoding failed")
    value = {"source": arguments.video, "rounds": detector.rounds, "warnings": detector.warnings,
             "observations": observations}
    Path(arguments.output).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rounds": len(detector.rounds), "warnings": detector.warnings}), flush=True)


if __name__ == "__main__":
    main()
