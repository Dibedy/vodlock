import sys
import time as clock
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))
from detector import HudReader

reader = HudReader()
capture = cv2.VideoCapture(sys.argv[1])
for time in map(float, sys.argv[2:]):
    capture.set(cv2.CAP_PROP_POS_MSEC, time * 1000)
    loaded, original = capture.read()
    if not loaded:
        raise ValueError("Missing frame")
    for width in [960, 1280]:
        height = width * 9 // 16
        frame = cv2.resize(original, (width, height))
        top = frame[:int(height * .09), int(width * .43):int(width * .57)]
        for factor in [2, 4]:
            result, _ = reader.engine(cv2.resize(top, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC), use_cls=False)
            print(time, width, factor, "full", [(text, round(float(score), 3)) for _, text, score in result or []], flush=True)
        label = frame[:int(height * .026), int(width * .46):int(width * .54)]
        result, _ = reader.engine(cv2.resize(label, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC), use_det=False, use_cls=False)
        print(time, width, "label", result, flush=True)
        timer = frame[int(height * .026):int(height * .065), int(width * .465):int(width * .535)]
        started = clock.perf_counter()
        result, _ = reader.engine(cv2.resize(timer, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), use_det=False, use_cls=False)
        print(time, width, "timer", result, "seconds", clock.perf_counter() - started, flush=True)
capture.release()
