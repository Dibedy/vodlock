import argparse
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))
from detector import HudReader


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job")
    arguments = parser.parse_args()
    job = json.loads(Path(arguments.job).read_text(encoding="utf-8"))
    reader = HudReader()
    capture = cv2.VideoCapture(job["source"])
    results = []
    try:
        for entry in job["rounds"]:
            samples = []
            for offset in [-5, 4]:
                time = max(0, entry["start"] + offset)
                capture.set(cv2.CAP_PROP_POS_MSEC, time * 1000)
                loaded, frame = capture.read()
                if not loaded:
                    raise ValueError(f"Could not inspect timestamp {time}.")
                frame = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)
                sample = reader.read(frame, time)
                samples.append({"time": time, "round": sample.round, "timer": sample.timer, "replay": sample.replay})
            live = samples[1]
            passed = live["round"] == entry["round"] and live["timer"] is not None and abs(live["timer"] - 96) <= 1 and not live["replay"]
            result = {"map": entry["map"], "round": entry["round"], "start": entry["start"], "passed": passed, "samples": samples}
            results.append(result)
            print(json.dumps(result), flush=True)
    finally:
        capture.release()
    print(json.dumps({"checked": len(results), "passed": sum(result["passed"] for result in results), "warnings": job["warnings"]}), flush=True)


if __name__ == "__main__":
    main()
