import argparse
import json
import sys
import time as clock
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))
from detector import HudReader


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("times", nargs="+", type=float)
    parser.add_argument("--images")
    arguments = parser.parse_args()
    capture = cv2.VideoCapture(arguments.video)
    if not capture.isOpened():
        raise ValueError("Could not open the inspection video.")
    reader = HudReader()
    output = Path(arguments.images) if arguments.images else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    try:
        for time in arguments.times:
            capture.set(cv2.CAP_PROP_POS_MSEC, time * 1000)
            loaded, frame = capture.read()
            if not loaded:
                raise ValueError(f"Could not read the frame at {time} seconds.")
            frame = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)
            started = clock.perf_counter()
            sample = reader.read(frame, time)
            elapsed = clock.perf_counter() - started
            lines = reader.read_lines(frame[:64, 550:730])
            print(json.dumps({"time": time, "round": sample.round, "timer": sample.timer,
                              "replay": sample.replay, "confidence": sample.confidence, "seconds": elapsed, "lines": lines}), flush=True)
            if output:
                destination = output / (str(time).replace(".", "-") + ".jpg")
                if not cv2.imwrite(str(destination), frame):
                    raise ValueError(f"Could not save the inspection frame {destination}.")
    finally:
        capture.release()


if __name__ == "__main__":
    main()
