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
    parser.add_argument("--output")
    arguments = parser.parse_args()
    job = json.loads(Path(arguments.job).read_text(encoding="utf-8"))
    reader = HudReader()
    capture = cv2.VideoCapture(job["source"])
    results = []
    try:
        for entry in job["rounds"]:
            if entry.get("excluded"):
                continue
            samples = []
            for offset in [-5, 4, 6, 8, 10, 12]:
                time = max(0, entry["start"] + offset)
                capture.set(cv2.CAP_PROP_POS_MSEC, time * 1000)
                loaded, frame = capture.read()
                if not loaded:
                    raise ValueError(f"Could not inspect timestamp {time}.")
                frame = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)
                sample = reader.read(frame, time)
                samples.append({"time": time, "round": sample.round, "timer": sample.timer, "replay": sample.replay})
            passed = sum(sample["round"] == entry["round"] and sample["timer"] is not None
                         and abs(sample["timer"] - (100 - offset)) <= 1 and not sample["replay"]
                         for offset, sample in zip([4, 6, 8, 10, 12], samples[1:])) >= 2
            result = {"map": entry["map"], "round": entry["round"], "start": entry["start"], "passed": passed, "samples": samples}
            results.append(result)
            print(json.dumps(result), flush=True)
    finally:
        capture.release()
    summary = {"checked": len(results), "passed": sum(result["passed"] for result in results), "warnings": job["warnings"]}
    if arguments.output:
        Path(arguments.output).write_text(json.dumps({**summary, "results": results}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
