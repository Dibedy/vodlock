import re
import statistics
from dataclasses import dataclass


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
        match = re.search(r"R[0O]UND\s*([0-9S]{1,2})\b", text)
        if match and 1 <= int(match[1].replace("S", "5")) <= 60:
            round_number = int(match[1].replace("S", "5"))
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

    def observe(self, sample):
        if sample.replay or sample.round is None or sample.timer is None or sample.confidence < 0.65:
            self.pending = []
            return
        if not 85 <= sample.timer <= 100:
            self.pending = []
            return
        if self.rounds:
            previous = self.rounds[-1]
            if sample.round == previous["round"]:
                return
            if sample.round < previous["round"]:
                if sample.round != 1 or previous["round"] < 12 or sample.time - previous["start"] < 120:
                    self.pending = []
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
        if len(self.pending) < 2:
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

    def read(self, frame, time):
        height, width = frame.shape[:2]
        label = frame[:int(height * 0.026), int(width * 0.46):int(width * 0.54)]
        clock = frame[int(height * 0.026):int(height * 0.065), int(width * 0.465):int(width * 0.535)]
        lines = self.read_lines(label, single=True, scale=4) + self.read_lines(clock, single=True)
        sample = parse_hud(time, lines)
        if sample.timer is not None and 85 <= sample.timer <= 100 and (sample.round is None or sample.confidence < 0.65):
            top = frame[:int(height * 0.09), int(width * 0.43):int(width * 0.57)]
            lines = self.read_lines(top)
            sample = parse_hud(time, lines)
        if sample.round is None or sample.timer is None or not 85 <= sample.timer <= 100:
            return sample
        replay = frame[int(height * 0.84):, int(width * 0.73):]
        return parse_hud(time, lines, self.read_lines(replay))
