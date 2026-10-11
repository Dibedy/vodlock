import math
import statistics
from dataclasses import asdict

from detector import Observation


DETECTOR_VERSION = "canonical-clock-v5"


class CandidateDetector:
    def __init__(self, state=None):
        state = state or {}
        self.pending = list(state.get("pending", []))
        self.previous = state.get("previous")
        self.map_number = int(state.get("map_number", 1))
        self.last_time = float(state.get("last_time", -1))
        self.map_reset = state.get("map_reset")
        self.confirmed_candidates = []
        self.scan = dict(state.get("scan", {}))

    def state(self):
        return {
            "pending": self.pending,
            "previous": self.previous,
            "map_number": self.map_number,
            "last_time": self.last_time,
            "map_reset": self.map_reset,
            "version": DETECTOR_VERSION,
            "scan": self.scan,
        }

    def observe(self, sample, wall_time=None, extra=None):
        self.confirmed_candidates = []
        evidence = {**asdict(sample), **(extra or {})}
        candidate = {
            "detector_version": DETECTOR_VERSION,
            "media_time": sample.time,
            "wall_time": wall_time,
            "round_number": sample.round,
            "timer": sample.timer,
            "scores": sample.scores,
            "replay": sample.replay,
            "confidence": sample.confidence,
            "accepted": False,
            "evidence": evidence,
            "findings": [],
        }
        if not math.isfinite(sample.time) or sample.time < 0:
            raise ValueError("Media timestamp must be finite and nonnegative")
        if sample.time <= self.last_time:
            candidate["findings"].append("duplicate_observation")
            return candidate
        self.last_time = sample.time
        if self.pending and sample.time - self.pending[0]["time"] > 7:
            self.pending = []
        if (extra or {}).get("probe_only"):
            candidate["findings"].append("probe_only")
            return candidate
        intro = bool((extra or {}).get("intro_context", {}) and (extra or {})["intro_context"].get("intro"))
        if intro:
            candidate["replay"] = True
            evidence["replay"] = True
        if intro or sample.replay or sample.buy_phase:
            self.pending = []
            candidate["findings"].append("intro_hud" if intro else "replay" if sample.replay else "buy_phase")
            return candidate
        if sample.round is None or sample.timer is None or sample.confidence < 0.85:
            candidate["findings"].append("unreadable_hud")
            return candidate
        if not 85 <= sample.timer <= 100:
            self.pending = []
            return candidate
        if sample.scores and sum(sample.scores) + 1 != sample.round:
            self.pending = []
            candidate["findings"].append("score_round_disagreement")
            return candidate
        if self.map_reset and sample.round == self.map_reset["round_number"]:
            candidate["findings"].append("pending_map_reset")
            return candidate
        if self.previous and sample.round == self.previous["round"] and not self.map_reset:
            candidate["findings"].append("duplicate_round")
            return candidate
        if self.previous and sample.round < self.previous["round"] and sample.round > 3 and not self.map_reset:
            candidate["findings"].append("backwards_numbering")
            return candidate
        start = sample.time - (100 - sample.timer)
        if self.pending:
            previous = self.pending[-1]
            elapsed = sample.time - previous["time"]
            decrease = previous["timer"] - sample.timer
            scores_changed = (
                sample.scores and previous.get("scores") and list(sample.scores) != list(previous["scores"])
            )
            if sample.round == previous["round"] and not scores_changed and decrease == 0 and elapsed <= 1:
                candidate["findings"].append("repeated_clock")
                return candidate
            if (
                sample.round != previous["round"]
                or scores_changed
                or decrease <= 0
                or abs(elapsed - decrease) > 1
                or abs(start - previous["start"]) > 1
            ):
                self.pending = []
                candidate["findings"].append("unstable_clock")
        self.pending.append(
            {
                "time": sample.time,
                "timer": sample.timer,
                "round": sample.round,
                "start": start,
                "scores": sample.scores,
                "confidence": sample.confidence,
                "teams": (extra or {}).get("teams"),
            }
        )
        if len(self.pending) < 3:
            return candidate
        start = max(0, statistics.median(item["start"] for item in self.pending))
        if self.previous:
            if start - self.previous["start"] < 20:
                candidate["findings"].append("suspicious_spacing")
                self.pending = []
                return candidate
        candidate["accepted"] = True
        evidence.update(
            start=round(start, 3),
            broadcast_map=self.map_number,
            agreement=list(self.pending),
            anchor="decreasing_gameplay_clock",
        )
        team_pairs = [tuple(sorted(item["teams"])) for item in self.pending if item.get("teams")]
        evidence["teams"] = next((list(pair) for pair in team_pairs if team_pairs.count(pair) >= 2), None)
        if self.previous and (sample.round < self.previous["round"] or self.map_reset):
            reset = self.map_reset
            if reset and sample.round == reset["round_number"] + 1 and start - reset["evidence"]["start"] >= 20:
                self.map_number += 1
                reset["accepted"] = True
                reset["findings"] = []
                reset["evidence"].update(broadcast_map=self.map_number, map_reset=True, reset_confirmation_time=sample.time)
                self.confirmed_candidates = [reset]
                self.map_reset = None
                evidence["broadcast_map"] = self.map_number
            elif sample.round == 1 and self.previous["round"] >= 13 and sample.scores == (0, 0):
                self.map_number += 1
                self.map_reset = None
                evidence.update(broadcast_map=self.map_number, map_reset=True)
            else:
                candidate["accepted"] = False
                candidate["findings"].append("pending_map_reset")
                if sample.round <= 3:
                    self.map_reset = candidate
                self.pending = []
                return candidate
        self.previous = {"round": sample.round, "start": start, "scores": sample.scores}
        self.pending = []
        return candidate


def observation_from_dict(value):
    return Observation(
        value["time"],
        value.get("round"),
        value.get("timer"),
        value.get("confidence", 0),
        value.get("replay", False),
        tuple(value["scores"]) if value.get("scores") is not None else None,
        tuple(value.get("raw_lines", [])),
        value.get("buy_phase", False),
    )


def redetect_candidates(candidates, anchors=()):
    detected = []
    states: dict[str, CandidateDetector] = {}
    originals = {}
    observations: dict[tuple[str, float], dict] = {}
    anchor_positions: dict[str, int] = {}
    ordered_anchors: dict[str, list[dict]] = {}
    for anchor in anchors:
        ordered_anchors.setdefault(anchor["timeline"], []).append(anchor)
    for timeline in ordered_anchors:
        ordered_anchors[timeline].sort(key=lambda value: value["media_time"])
    for candidate in candidates:
        key = (candidate["timeline"], candidate["media_time"])
        previous = observations.get(key)
        if previous is None or (candidate["confidence"], candidate["detector_version"], str(candidate.get("id", ""))) > (
            previous["confidence"], previous["detector_version"], str(previous.get("id", ""))
        ):
            observations[key] = candidate
    intro_ranges = [
        (candidate["timeline"], candidate["media_time"] - (100 - candidate["timer"]), candidate["media_time"] + max(15, (candidate["evidence"].get("intro_context") or {}).get("countdown_seconds", 0)))
        for candidate in candidates
        if (candidate["evidence"].get("intro_context") or {}).get("intro") and candidate.get("timer") is not None
    ]
    for candidate in sorted(observations.values(), key=lambda value: value["media_time"]):
        evidence = candidate["evidence"]
        timeline = candidate["timeline"]
        originals[(timeline, candidate["media_time"])] = candidate
        if "time" not in evidence or evidence.get("provenance", {}).get("method") == "piecewise_secondary_recovery":
            if candidate["accepted"]:
                detected.append(candidate)
            continue
        if timeline not in states:
            states[timeline] = CandidateDetector()
        detector = states[timeline]
        position = anchor_positions.get(timeline, 0)
        timeline_anchors = ordered_anchors.get(timeline, [])
        while position < len(timeline_anchors) and timeline_anchors[position]["media_time"] < candidate["media_time"]:
            anchor = timeline_anchors[position]
            if detector.previous is None or anchor["evidence"]["start"] > detector.previous["start"]:
                detector.previous = {"round": anchor["round_number"], "start": anchor["evidence"]["start"], "scores": anchor["scores"]}
                detector.map_number = anchor["evidence"]["broadcast_map"]
                detector.pending = []
                detector.map_reset = None
            position += 1
        anchor_positions[timeline] = position
        if candidate["media_time"] <= detector.last_time:
            continue
        intro = any(group == timeline and lower <= candidate["media_time"] <= upper for group, lower, upper in intro_ranges)
        observation = observation_from_dict({**evidence, "time": candidate["media_time"], "replay": evidence.get("replay", False) or intro})
        wall_time = candidate.get("wall_time")
        value = detector.observe(observation, wall_time.isoformat() if wall_time is not None and hasattr(wall_time, "isoformat") else wall_time, extra={"teams": evidence.get("teams"), "intro_context": evidence.get("intro_context"), "probe_only": evidence.get("probe_only", False)})
        for accepted in detector.confirmed_candidates + [value]:
            if accepted["accepted"]:
                original = originals[(timeline, accepted["media_time"])]
                detected.append({
                    **original, **accepted,
                    "evidence": {**accepted["evidence"], "provenance": original["evidence"].get("provenance", {"method": "live_official_youtube"})},
                })
    return detected, states
