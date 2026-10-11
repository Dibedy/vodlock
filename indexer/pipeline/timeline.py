import statistics
import math
from bisect import bisect_left, bisect_right
from typing import Any

from storyboard_align import align_storyboards, frame_distance, matching_frames

from .errors import NeedsReview


def scoreboard_section(samples, expected, canonical_start, scale):
    visible = [sample for sample in samples if sample.round == expected["round"]
               and sample.scores is not None and sample.confidence >= .85 and not sample.replay
               and sum(sample.scores) == expected["round"] - 1
               and (not expected.get("scores") or sorted(sample.scores) == sorted(expected["scores"]))]
    clocks = [sample for sample in visible if sample.timer is not None and 85 <= sample.timer <= 100
              and not sample.buy_phase]
    for seed in clocks:
        start = seed.time - (100 - seed.timer)
        anchors = [sample for sample in clocks if abs(sample.time - (100 - sample.timer) - start) <= 1]
        anchors.sort(key=lambda sample: sample.time)
        if len(anchors) < 5 or anchors[-1].time - anchors[0].time < 4:
            continue
        if any(right.time <= left.time or right.timer >= left.timer for left, right in zip(anchors, anchors[1:])):
            continue
        start = statistics.median(sample.time - (100 - sample.timer) for sample in anchors)
        lead = [sample for sample in visible if start - 7 <= sample.time <= start - 5]
        if len(lead) < 2 or start < 7 or not math.isfinite(start):
            continue
        coverage = sorted((sample.time for sample in visible if lead[-1].time <= sample.time <= start + 3))
        if not coverage or coverage[-1] < start + 2 or any(right - left > 2 for left, right in zip(coverage, coverage[1:])):
            continue
        offset = canonical_start - scale * start
        return {"sourceStart": start - 5.5, "sourceEnd": start + 3,
                "canonicalStart": canonical_start - 5.5 * scale, "canonicalEnd": canonical_start + 3 * scale,
                "offset": offset, "anchors": len(anchors),
                "maximumResidual": max(abs(sample.time - (100 - sample.timer) - start) * scale for sample in anchors),
                "method": "scoreboard"}
    return None


def scoreboard_alignment(mapping, checks):
    scale = mapping["timelineScale"]
    verified = sorted((check["section"] for check in checks.values() if check.get("section")),
                      key=lambda section: section["sourceStart"])
    for left, right in zip(verified, verified[1:]):
        if left["sourceEnd"] >= right["sourceStart"] or left["canonicalEnd"] >= right["canonicalStart"]:
            raise NeedsReview("Watch-party scoreboard checks have conflicting round order")
    sections: list[dict] = []
    for section in mapping["segments"]:
        fragments = [(section["sourceStart"], section["sourceEnd"])]
        for check in verified:
            lower = min(check["sourceStart"], (check["canonicalStart"] - section["offset"]) / scale)
            upper = max(check["sourceEnd"], (check["canonicalEnd"] - section["offset"]) / scale)
            fragments = [(start, end) for first, last in fragments
                         for start, end in [(first, min(last, lower)), (max(first, upper), last)] if end > start]
        sections.extend({**section, "sourceStart": first, "sourceEnd": last,
                         "canonicalStart": scale * first + section["offset"],
                         "canonicalEnd": scale * last + section["offset"]} for first, last in fragments)
    sections = sorted(sections + verified, key=lambda section: section["sourceStart"])
    return {**mapping, "segments": sections, "roundChecks": checks}


def local_alignment(canonical, source, maximum_distance=12, maximum_residual=2):
    matches = []
    for frame, target, distance in matching_frames(canonical["frames"] if len(canonical["frames"]) >= 2 else [], source["frames"], maximum_distance):
        matches.append({"source": frame["time"], "canonical": target["time"], "offset": target["time"] - frame["time"], "distance": distance})
    ordered = sorted(matches, key=lambda value: value["source"])
    source_times = [value["source"] for value in ordered]
    slopes = [(right["canonical"] - left["canonical"]) / (right["source"] - left["source"])
              for index, left in enumerate(ordered)
              for right in ordered[bisect_left(source_times, left["source"] + 60, index + 1):bisect_right(source_times, left["source"] + 600, index + 1)]
              if 60 <= right["source"] - left["source"] <= 600
              and .95 <= (right["canonical"] - left["canonical"]) / (right["source"] - left["source"]) <= 1.05]
    scale = statistics.median(slopes) if slopes else 1
    for value in matches:
        value["offset"] = value["canonical"] - scale * value["source"]
    gap = max(60, 3 * source.get("interval", 10), 3 * canonical.get("interval", 10))
    sections = []
    seen_offsets = set()
    for seed in matches:
        if seed["offset"] in seen_offsets:
            continue
        seen_offsets.add(seed["offset"])
        anchors = [value for value in matches if abs(value["offset"] - seed["offset"]) <= maximum_residual]
        if len(anchors) < 5:
            continue
        offset = statistics.median(value["offset"] for value in anchors)
        anchors = sorted((value for value in anchors if abs(value["offset"] - offset) <= maximum_residual), key=lambda value: value["source"])
        runs: list[list[dict]] = [[]]
        for value in anchors:
            previous = runs[-1][-1] if runs[-1] else None
            if previous and (value["source"] - previous["source"] > gap or value["canonical"] <= previous["canonical"]):
                runs.append([])
            runs[-1].append(value)
        for run in runs:
            if len(run) >= 5 and run[-1]["source"] - run[0]["source"] >= 60:
                sections.append((offset, run))
    selected: list[dict] = []
    rejected = []
    for offset, anchors in sorted(sections, key=lambda value: len(value[1]), reverse=True):
        lower, upper = anchors[0]["source"], anchors[-1]["source"]
        if any(lower <= value["sourceEnd"] and upper >= value["sourceStart"] for value in selected):
            continue
        canonical_start, canonical_end = scale * lower + offset, scale * upper + offset
        if any(not (upper < value["sourceStart"] and canonical_end < value["canonicalStart"] or lower > value["sourceEnd"] and canonical_start > value["canonicalEnd"]) for value in selected):
            rejected.append({"sourceStart": lower, "sourceEnd": upper, "reason": "non_monotonic_visual_section"})
            continue
        selected.append({
            "sourceStart": lower, "sourceEnd": upper, "canonicalStart": scale * lower + offset,
            "canonicalEnd": scale * upper + offset, "offset": offset, "anchors": len(anchors),
            "maximumResidual": max(abs(value["offset"] - offset) for value in anchors),
        })
    selected.sort(key=lambda value: value["sourceStart"])
    if not selected:
        raise NeedsReview("Insufficient unique precise visual anchors for local alignment")
    return {"version": "canonical-local-v1", "timelineScale": scale, "segments": selected,
            "anchors": sum(value["anchors"] for value in selected), "maximumResidual": max(value["maximumResidual"] for value in selected), "direction": "source_to_canonical", "unverifiedSections": rejected}


def piecewise_alignment(canonical, source, maximum_distance=12, maximum_residual=2, local=False):
    if local:
        return local_alignment(canonical, source, maximum_distance, maximum_residual)
    base = min((frame["time"] for frame in source["frames"]), default=0)
    normalized = {
        **source,
        "duration": source["duration"] - base,
        "frames": [{**frame, "time": frame["time"] - base} for frame in source["frames"]],
    }
    try:
        result = align_storyboards(
            canonical,
            normalized,
            maximum_distance=maximum_distance,
            require_target_coverage=True,
            maximum_residual=maximum_residual,
        )
    except ValueError as error:
        raise NeedsReview("Visual timeline alignment requires review: " + str(error)) from error
    scale = result["timelineScale"]
    verified = []
    previous_canonical_end = -1.0
    canonical_frames = sorted(canonical["frames"], key=lambda frame: frame["time"])
    canonical_times = [frame["time"] for frame in canonical_frames]
    for segment in result["segments"]:
        segment = {**segment, "offset": segment["offset"] - scale * base}
        anchors = []
        for frame in source["frames"]:
            time = frame["time"]
            if not segment["targetStart"] <= scale * (time - base) <= segment["targetEnd"]:
                continue
            expected = scale * time + segment["offset"]
            matches = canonical_frames[
                bisect_left(canonical_times, expected - maximum_residual) : bisect_right(
                    canonical_times, expected + maximum_residual
                )
            ]
            if matches and min(frame_distance(frame, target) for target in matches) <= maximum_distance:
                anchors.append(time)
        if len(anchors) < 5:
            raise NeedsReview("Insufficient precise anchors in alignment section")
        start, end = min(anchors), max(anchors)
        if scale * start + segment["offset"] < previous_canonical_end:
            raise NeedsReview("Alignment reverses the canonical timeline")
        previous_canonical_end = scale * end + segment["offset"]
        verified.append(
            {
                **segment,
                "sourceStart": start,
                "sourceEnd": end,
                "canonicalStart": scale * start + segment["offset"],
                "canonicalEnd": previous_canonical_end,
            }
        )
    return {**result, "segments": verified, "direction": "source_to_canonical"}


def mapped_time(time, alignment):
    for segment in alignment["segments"]:
        if segment["sourceStart"] <= time <= segment["sourceEnd"]:
            return round(alignment["timelineScale"] * time + segment["offset"], 3)
    raise NeedsReview(f"Timestamp {time} lies outside verified visual anchors")


def reconcile_rounds(rounds, alignment):
    result = []
    for item in rounds:
        start = mapped_time(item["start"], alignment)
        if start < 0:
            raise NeedsReview("Archive trim removed an indexed round")
        result.append({**item, "start": start, "live_start": item.get("live_start", item["start"])})
    return result


def validate_rounds(rounds, best_of=None, minimum_confidence=0.85):
    findings = []
    seen = set()
    previous = None
    maps: dict[int, list[dict]] = {}
    for item in rounds:
        if not isinstance(item["start"], (int, float)) or isinstance(item["start"], bool) or not math.isfinite(item["start"]) or item["start"] < 0:
            findings.append({"code": "invalid_timestamp", "map": item["map"], "round": item["round"]})
            continue
        key = (item["map"], item["round"])
        if key in seen:
            findings.append({"code": "duplicate_round", "map": key[0], "round": key[1]})
        seen.add(key)
        maps.setdefault(item["map"], []).append(item)
        if item.get("confidence", 0) < minimum_confidence or item.get("replay"):
            findings.append({"code": "untrusted_round", "map": key[0], "round": key[1]})
        scores = item.get("scores")
        if scores and (any(score < 0 for score in scores) or sum(scores) != item["round"] - 1):
            findings.append({"code": "impossible_score", "map": key[0], "round": key[1]})
        if previous:
            if item["start"] <= previous["start"] + 20:
                findings.append({"code": "suspicious_spacing", "start": item["start"]})
            if item["map"] == previous["map"]:
                if item["round"] != previous["round"] + 1:
                    findings.append(
                        {
                            "code": "missing_rounds" if item["round"] > previous["round"] else "backwards_numbering",
                            "map": item["map"],
                            "after": previous["round"],
                            "before": item["round"],
                            "range": [max(0, previous["start"] - 10), item["start"] + 15],
                        }
                    )
                previous_scores = previous.get("scores")
                if scores and previous_scores and item["round"] == previous["round"] + 1:
                    differences = sorted(
                        abs(left - right) for left, right in zip(sorted(scores), sorted(previous_scores))
                    )
                    if differences != [0, 1]:
                        findings.append({"code": "impossible_score_change", "map": item["map"], "round": item["round"]})
            elif item["map"] != previous["map"] + 1 or item["round"] != 1 or previous["round"] < 13:
                findings.append(
                    {
                        "code": "invalid_map_sequence",
                        "map": item["map"],
                        "range": [max(0, previous["start"] - 10), item["start"] + 15],
                    }
                )
        previous = item
    if not rounds or rounds[0]["map"] != 1 or rounds[0]["round"] != 1:
        start = rounds[0]["start"] if rounds else None
        end = start + 15 if isinstance(start, (int, float)) and math.isfinite(start) and start >= 0 else 900
        findings.append({"code": "late_start", "range": [0, end]})
    for map_number, entries in maps.items():
        scores = entries[-1].get("scores")
        can_finish = not scores or any(
            scores[side] + 1 >= 13 and scores[side] + 1 - scores[1 - side] >= 2 for side in (0, 1)
        )
        if len(entries) < 13 or not can_finish:
            finding = {"code": "incomplete_map", "map": map_number}
            following = maps.get(map_number + 1)
            if following:
                finding["range"] = [max(0, entries[-1]["start"] - 10), following[0]["start"] + 15]
            findings.append(finding)
    if best_of and (len(maps) < best_of // 2 + 1 or len(maps) > best_of):
        findings.append({"code": "incomplete_series", "maps": len(maps), "best_of": best_of})
    return findings


def finding_severity(code):
    if code in {"duplicate_observation", "replay", "buy_phase", "intro_hud", "repeated_clock", "probe_only"}:
        return "info"
    if code in {"unreadable_hud", "unstable_clock", "pending_map_reset"}:
        return "warning"
    if code in {"duplicate_round", "impossible_score", "impossible_score_change", "backwards_numbering", "invalid_timestamp"}:
        return "error"
    return "blocking"


def broadcast_rounds(candidates):
    ranks = {"live_official_youtube": 0, "direct_official_archive": 1, "piecewise_secondary_recovery": 2}
    ordered: list[dict] = []
    for item in sorted(candidates, key=lambda value: value["start"]):
        nearby = next(
            (
                previous
                for previous in reversed(ordered[-3:])
                if previous["round"] == item["round"] and abs(previous["start"] - item["start"]) <= 3
            ),
            None,
        )
        if nearby:
            rank = ranks.get(item["provenance"]["method"], 3)
            previous_rank = ranks.get(nearby["provenance"]["method"], 3)
            if rank < previous_rank or rank == previous_rank and item["confidence"] > nearby["confidence"]:
                ordered[ordered.index(nearby)] = item
        else:
            ordered.append(item)
    ordered.sort(key=lambda value: value["start"])
    map_number = 1
    previous = None
    for item in ordered:
        scores = item.get("scores")
        if previous and item["round"] < previous["round"] and (
            item.get("map_reset") or previous["round"] >= 13 and scores and sum(scores) < 3
        ):
            map_number += 1
        item["map"] = map_number
        previous = item
    return ordered


def segment_matches(rounds, expected):
    segments: list[tuple[list[dict], tuple | None]] = []
    current: list[dict] = []
    identity: tuple | None = None
    ordered = sorted(rounds, key=lambda value: value["start"])
    map_identities: dict[int, dict[tuple, int]] = {}
    for item in ordered:
        pair = tuple(sorted(item.get("teams") or []))
        if pair:
            identities = map_identities.setdefault(item["map"], {})
            identities[pair] = identities.get(pair, 0) + 1
    for item in ordered:
        teams = tuple(sorted(item.get("teams") or [])) or None
        reset = bool(
            current
            and item["round"] == 1
            and item["map"] > current[-1]["map"]
            and (item.get("scores") in ([0, 0], (0, 0)) or item.get("map_reset"))
        )
        if reset and teams is None:
            identities = map_identities.get(item["map"], {})
            if len(identities) == 1:
                pair, count = next(iter(identities.items()))
                if count >= 2:
                    teams = pair
        changed_teams = bool(identity and teams and teams != identity)
        if reset and changed_teams:
            segments.append((current, identity))
            current = []
            identity = None
        current.append(item)
        if teams:
            identity = teams if identity is None else identity
    if current:
        segments.append((current, identity))
    result = []
    assigned = set()
    for position, (entries, teams) in enumerate(segments):
        matches = [match for match in expected if teams and tuple(sorted((match["team_a"], match["team_b"]))) == teams]
        match = matches[0] if len(matches) == 1 else None
        findings = []
        if match and match["id"] in assigned:
            match = None
        if match:
            assigned.add(match["id"])
            if match["match_order"] != position + 1:
                findings.append({"code": "schedule_order_disagreement", "detected_order": position + 1})
        else:
            findings.append({"code": "unassigned_match", "detected_teams": teams})
        first_map = entries[0]["map"]
        normalized = [{**item, "map": item["map"] - first_map + 1} for item in entries]
        findings.extend(validate_rounds(normalized, match["best_of"] if match else None))
        result.append(
            {
                "match": match,
                "rounds": normalized,
                "start": entries[0]["start"],
                "end": entries[-1]["start"],
                "findings": findings,
                "state": "needs_review" if findings else "validating",
                "evidence": {
                    "closed": position + 1 < len(segments),
                    "teams": teams,
                    "detected_order": position + 1,
                    "boundary_signals": ["round_reset", "confirmed_map_reset" if entries[0].get("map_reset") else "score_reset", "team_change"]
                    if position
                    else ["coherent_rounds", "team_identity"],
                },
            }
        )
    return result


def compare_indexes(legacy, shadow):
    report: dict[str, Any] = {
        "missing_matches": sorted(set(legacy) - set(shadow)),
        "new_matches": sorted(set(shadow) - set(legacy)),
        "matches": {},
        "segmentation_disagreements": [],
        "median_difference": None,
        "worst_difference": None,
    }
    differences: list[float] = []
    for match_id in sorted(set(legacy) & set(shadow)):
        left, right = legacy[match_id], shadow[match_id]
        old = {(item["map"], item["round"]): item["start"] for item in left["rounds"]}
        new = {(item["map"], item["round"]): item["start"] for item in right["rounds"]}
        changes = [
            {"map": key[0], "round": key[1], "difference": round(new[key] - old[key], 3)}
            for key in sorted(old.keys() & new.keys())
        ]
        differences.extend(abs(item["difference"]) for item in changes)
        report["matches"][match_id] = {
            "missing_maps": sorted({key[0] for key in old} - {key[0] for key in new}),
            "missing_rounds": sorted(old.keys() - new.keys()),
            "extra_rounds": sorted(new.keys() - old.keys()),
            "duplicates": len(right["rounds"]) - len(new),
            "differences": changes,
        }
        if left.get("sourceId") != right.get("sourceId") or {key[0] for key in old} != {key[0] for key in new}:
            report["segmentation_disagreements"].append(match_id)
    if differences:
        report["median_difference"] = statistics.median(differences)
        report["worst_difference"] = max(differences)
    return report
