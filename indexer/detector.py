import re
import statistics
from dataclasses import dataclass


DETECTOR_VERSION = "vct-clock-ocr-v7"
HUD_PROFILES = (
    {"label": (0, .026, .46, .54), "clock": (.026, .065, .465, .535),
     "top": (0, .034, .445, .555), "wide": (0, .09, .43, .57), "score_top": .053},
    {"label": (.006, .036, .45, .55), "clock": (.036, .078, .46, .54),
     "top": (.006, .046, .435, .565), "wide": (0, .105, .42, .58), "score_top": .064},
    {"label": (.012, .046, .44, .56), "clock": (.046, .09, .455, .545),
     "top": (.012, .058, .425, .575), "wide": (0, .12, .41, .59), "score_top": .076},
)


@dataclass
class Observation:
    time: float
    round: int | None
    timer: int | None
    confidence: float = 0.0
    replay: bool = False


def parse_hud(time, lines, replay_lines=()):
    round_number = None
    timer = None
    confidences = []
    for text, confidence in lines:
        if confidence < 0.65:
            continue
        text = text.upper().replace("O", "0")
        match = re.search(r"R[0O]UND\s*([0-9SGB]{1,2})\b", text)
        number = match[1].translate(str.maketrans({"S": "5", "G": "6", "B": "8"})) if match else ""
        if number and 1 <= int(number) <= 60:
            round_number = int(number)
            confidences.append(confidence)
        match = re.fullmatch(r"\s*([01])\s*[:.]\s*([0-5][0-9])\s*", text)
        if match:
            timer = int(match[1]) * 60 + int(match[2])
            confidences.append(confidence)
    replay = any("REPLAY" in text.upper().replace(" ", "") and score >= 0.55
                 for text, score in replay_lines)
    return Observation(time, round_number, timer, min(confidences, default=0), replay)


class RoundDetector:
    def __init__(self):
        self.rounds = []
        self.pending = []
        self.map_number = 1
        self.warnings = []

    def finalize(self):
        if len(self.rounds) > 1 and self.rounds[-1]["round"] == 1 and self.rounds[-1]["map"] > self.rounds[-2]["map"]:
            self.rounds[-1]["excluded"] = True

    def observe(self, sample):
        if self.pending and sample.time - self.pending[0].time > 7:
            self.pending = []
        if sample.replay:
            self.pending = []
            return
        if sample.round is None or sample.timer is None or sample.confidence < 0.65:
            return
        if not 85 <= sample.timer <= 100:
            return
        if self.rounds:
            previous = self.rounds[-1]
            if sample.round == previous["round"]:
                return
            if sample.round < previous["round"]:
                if sample.round != 1 or previous["round"] < 12 or sample.time - previous["start"] < 120:
                    return
        start = sample.time - (100 - sample.timer)
        if start < -2:
            return
        if self.pending:
            first = self.pending[0]
            expected = first.time - (100 - first.timer)
            if sample.round != first.round or sample.time - first.time > 7 or abs(start - expected) > 2.5:
                self.pending = []
            elif sample.time <= first.time or sample.timer >= first.timer:
                self.pending = [sample]
                return
        self.pending.append(sample)
        required = 3 if self.rounds and sample.round not in {1, self.rounds[-1]["round"] + 1} else 2
        if len(self.pending) < required:
            return
        start = max(0, statistics.median(s.time - (100 - s.timer) for s in self.pending))
        if self.rounds:
            previous = self.rounds[-1]
            if start <= previous["start"] + 15:
                self.pending = []
                return
            if sample.round == 1 and previous["round"] >= 12:
                self.map_number += 1
            elif sample.round != previous["round"] + 1:
                self.warnings.append(f"Map {self.map_number}: check the gap before round {sample.round}.")
        elif sample.round != 1:
            self.warnings.append("The first detected round is not round 1. Check the beginning of this recording.")
        self.rounds.append({"map": self.map_number, "round": sample.round, "start": round(start, 2),
                            "confidence": round(min(s.confidence for s in self.pending), 3), "verified": False})
        self.pending = []


class HudReader:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self.engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1,
                               det_limit_type="max", det_limit_side_len=512)

    def read_lines(self, image, single=False, scale=2):
        import cv2
        enlarged = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        result, _ = self.engine(enlarged, use_det=not single, use_cls=False)
        if single:
            return [(text, float(score)) for text, score in result or []]
        return [(text, float(score)) for _, text, score in result or []]

    def crop(self, frame, bounds):
        height, width = frame.shape[:2]
        top, bottom, left, right = bounds
        return frame[int(height * top):int(height * bottom), int(width * left):int(width * right)]

    def read_profile(self, frame, time, profile):
        height, width = frame.shape[:2]
        label = self.crop(frame, profile["label"])
        clock = self.crop(frame, profile["clock"])
        clock_lines = self.read_lines(clock, single=True)
        lines = self.read_lines(label, single=True, scale=4) + clock_lines
        sample = parse_hud(time, lines)
        if sample.timer is not None and 85 <= sample.timer <= 100:
            top = self.crop(frame, profile["top"])
            top_lines = self.read_lines(top, scale=4)
            if parse_hud(time, top_lines).round is None:
                top = self.crop(frame, profile["wide"])
                top_lines = self.read_lines(top)
            if parse_hud(time, top_lines).round is not None:
                lines = top_lines + clock_lines
                sample = parse_hud(time, lines)
            scores = []
            for left, right in [(0.417, 0.44), (0.56, 0.583)]:
                score = frame[:int(height * profile["score_top"]), int(frame.shape[1] * left):int(frame.shape[1] * right)]
                result = self.read_lines(score, single=True, scale=3)
                if len(result) != 1 or result[0][1] < 0.95 or not re.fullmatch(r"[0-9]{1,2}", result[0][0].strip()):
                    break
                scores.append((int(result[0][0]), result[0][1]))
            if len(scores) == 2 and 1 <= sum(value for value, _ in scores) + 1 <= 60:
                sample.round = sum(value for value, _ in scores) + 1
                sample.confidence = min(parse_hud(time, clock_lines).confidence, *(confidence for _, confidence in scores))
        if sample.round is None or sample.timer is None or not 85 <= sample.timer <= 100:
            return sample
        replay = frame[int(height * 0.84):, int(width * 0.73):]
        sample.replay = parse_hud(time, (), self.read_lines(replay)).replay
        return sample

    def read(self, frame, time):
        best = None
        for profile in HUD_PROFILES:
            sample = self.read_profile(frame, time, profile)
            if best is None or sample.confidence > best.confidence:
                best = sample
            if sample.round is not None and sample.timer is not None:
                return sample
        return best
