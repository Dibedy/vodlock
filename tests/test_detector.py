import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))
from detector import HUD_PROFILES, HudReader, Observation, RoundDetector, parse_hud


class DetectorTests(unittest.TestCase):
    def test_seeded_detector_can_verify_a_local_mid_map_window(self):
        seed = {"map": 2, "round": 8, "start": 1000, "confidence": .9, "verified": False}
        detector = RoundDetector(seed=seed)
        for second, timer in [(1110, 100), (1111, 99)]:
            detector.observe(Observation(second, 9, timer, .95))
        self.assertEqual([(item["map"], item["round"], item["start"]) for item in detector.rounds],
                         [(2, 8, 1000), (2, 9, 1110)])
        self.assertEqual(detector.warnings, [])

    def test_parses_broadcast_hud(self):
        sample = parse_hud(10, [("ROUND 16", .95), ("0:56", .98)])
        self.assertEqual((sample.round, sample.timer), (16, 56))

    def test_normalizes_observed_round_five_ocr_confusion(self):
        sample = parse_hud(430, [("ROUNDS", .98), ("1:39", .99)])
        self.assertEqual((sample.round, sample.timer), (5, 99))

    def test_normalizes_observed_official_round_six_ocr_confusion(self):
        sample = parse_hud(3020, [("ROUNDG", .89), ("1:38", .98)])
        self.assertEqual((sample.round, sample.timer), (6, 98))

    def test_wider_label_read_corrects_confident_round_eight_misread(self):
        reader = HudReader.__new__(HudReader)
        with patch.object(reader, "read_lines", side_effect=[[("1:37", .99)], [("ROUNDS", .89)],
                                                            [("ROUND8", .93)], [("3", .99)], [("4", .99)], []]):
            sample = reader.read(np.zeros((720, 1280, 3), dtype=np.uint8), 3410)
        self.assertEqual((sample.round, sample.timer), (8, 97))

    def test_team_scores_correct_a_confident_round_label_error(self):
        reader = HudReader.__new__(HudReader)
        with patch.object(reader, "read_lines", side_effect=[[("1:36", .99)], [("ROUND18", .99)],
                                                            [("ROUND18", .99)], [("7", .99)], [("8", .99)], []]):
            sample = reader.read(np.zeros((720, 1280, 3), dtype=np.uint8), 4676)
        self.assertEqual((sample.round, sample.timer), (16, 96))

    def test_uncertain_scores_do_not_override_a_readable_round_label(self):
        reader = HudReader.__new__(HudReader)
        with patch.object(reader, "read_lines", side_effect=[[("1:36", .99)], [("ROUND16", .99)],
                                                            [("ROUND16", .99)], [("7", .8)], []]):
            sample = reader.read(np.zeros((720, 1280, 3), dtype=np.uint8), 4676)
        self.assertEqual((sample.round, sample.timer), (16, 96))

    def test_reader_uses_an_alternate_hud_profile_when_primary_is_unreadable(self):
        reader = HudReader.__new__(HudReader)
        unreadable = Observation(10, None, None)
        readable = Observation(10, 1, 99, .98)
        with patch.object(reader, "read_profile", side_effect=[unreadable, readable]) as profiles:
            sample = reader.read(np.zeros((720, 1280, 3), dtype=np.uint8), 10)
        self.assertIs(sample, readable)
        self.assertEqual(profiles.call_count, 2)

    def test_reader_stops_after_clock_when_round_start_is_impossible(self):
        reader = HudReader.__new__(HudReader)
        with patch.object(reader, "read_lines", return_value=[("0:42", .99)]) as reads:
            sample = reader.read_profile(np.zeros((720, 1280, 3), dtype=np.uint8), 10, HUD_PROFILES[0])
        self.assertEqual((sample.round, sample.timer), (None, 42))
        self.assertEqual(reads.call_count, 1)

    def test_compact_reader_uses_original_hud_coordinates(self):
        reader = HudReader.__new__(HudReader)
        frame = np.zeros((324, 1280, 3), dtype=np.uint8)
        clock = reader.crop(frame, HUD_PROFILES[0]["clock"], compact=True)
        replay = frame[208:324, 934:1280]
        self.assertEqual(clock.shape[:2], (28, 89))
        self.assertEqual(replay.shape[:2], (116, 346))

    def test_clock_probe_checks_other_profiles_after_an_irrelevant_timer(self):
        reader = HudReader.__new__(HudReader)
        with patch.object(reader, "read_lines", side_effect=[[('0:42', .99)], [('1:37', .98)]]) as reads:
            sample = reader.read_clock(np.zeros((324, 1280, 3), dtype=np.uint8), 10, compact=True)
        self.assertEqual(sample.timer, 97)
        self.assertEqual(reads.call_count, 2)

    def test_explicit_replay_is_rejected(self):
        sample = parse_hud(10, [("ROUND 16", .95), ("1:39", .98)], [("REPLAY", .9)])
        detector = RoundDetector()
        detector.observe(sample)
        detector.observe(Observation(12, 16, 97, .99, True))
        self.assertEqual(detector.rounds, [])

    def test_estimates_actual_start_not_confirmation_time(self):
        detector = RoundDetector()
        detector.observe(Observation(104, 1, 96, .99))
        detector.observe(Observation(106, 1, 94, .99))
        self.assertEqual(detector.rounds[0]["start"], 100)

    def test_preround_does_not_create_start(self):
        detector = RoundDetector()
        detector.observe(Observation(80, 1, 20, .99))
        detector.observe(Observation(82, 1, 18, .99))
        self.assertEqual(detector.rounds, [])

    def test_frozen_timer_is_not_a_round(self):
        detector = RoundDetector()
        detector.observe(Observation(100, 1, 99, .99))
        detector.observe(Observation(102, 1, 99, .99))
        self.assertEqual(detector.rounds, [])

    def test_one_frame_and_conflicting_clock_are_not_accepted(self):
        detector = RoundDetector()
        detector.observe(Observation(100, 1, 99, .99))
        detector.observe(Observation(102, 1, 90, .99))
        self.assertEqual(detector.rounds, [])

    def test_rounds_are_deduplicated(self):
        detector = RoundDetector()
        for time, timer in [(100, 100), (102, 98), (104, 96), (106, 94)]:
            detector.observe(Observation(time, 1, timer, .99))
        self.assertEqual(len(detector.rounds), 1)

    def test_matching_clocks_survive_an_unreadable_intermediate_frame(self):
        detector = RoundDetector()
        detector.observe(Observation(100, 1, 100, .99))
        detector.observe(Observation(102, None, None))
        detector.observe(Observation(104, 1, 96, .99))
        self.assertEqual(detector.rounds[0]["start"], 100)

    def test_backward_ocr_misread_does_not_discard_pending_next_round(self):
        detector = RoundDetector()
        detector.rounds = [{"map": 1, "round": 7, "start": 100}]
        detector.observe(Observation(200, 8, 100, .99))
        detector.observe(Observation(202, 5, 98, .99))
        detector.observe(Observation(204, 8, 96, .99))
        self.assertEqual(detector.rounds[-1]["round"], 8)
        self.assertEqual(detector.warnings, [])

    def test_unreadable_frames_do_not_extend_the_confirmation_window(self):
        detector = RoundDetector()
        detector.observe(Observation(100, 1, 100, .99))
        detector.observe(Observation(108, None, None))
        detector.observe(Observation(110, 1, 90, .99))
        self.assertEqual(detector.rounds, [])

    def test_isolated_preroll_detection_is_ignored_until_round_one(self):
        detector = RoundDetector()
        for time, number, timer in [(100, 8, 100), (102, 8, 98),
                                    (500, 1, 100), (502, 1, 98),
                                    (600, 2, 100), (602, 2, 98)]:
            detector.observe(Observation(time, number, timer, .99))
        self.assertEqual([entry["round"] for entry in detector.rounds], [1, 2])
        self.assertEqual(detector.warnings, [])

    def test_pre_round_one_sequence_remains_held_as_possible_midmatch_recording(self):
        detector = RoundDetector()
        for time, number, timer in [(100, 8, 100), (102, 8, 98),
                                    (200, 9, 100), (202, 9, 98),
                                    (500, 1, 100), (502, 1, 98)]:
            detector.observe(Observation(time, number, timer, .99))
        self.assertEqual([entry["round"] for entry in detector.rounds], [1])
        self.assertEqual(detector.warnings,
                         ["A round sequence was detected before round 1. Check whether this recording starts mid-match."])

    def test_official_day_archive_can_discard_preroll_before_its_first_complete_map(self):
        detector = RoundDetector(allow_preroll=True)
        for time, number, timer in [(100, 8, 100), (102, 8, 98),
                                    (200, 9, 100), (202, 9, 98),
                                    (500, 1, 100), (502, 1, 98)]:
            detector.observe(Observation(time, number, timer, .99))
        self.assertEqual([entry["round"] for entry in detector.rounds], [1])
        self.assertEqual(detector.warnings, [])

    def test_map_reset_and_missing_round_warning(self):
        detector = RoundDetector()
        for number, time, samples in [(1, 100, 2), (12, 300, 3), (14, 500, 3), (1, 1000, 2)]:
            for offset in range(samples):
                detector.observe(Observation(time + offset * 2, number, 100 - offset * 2, .99))
        self.assertEqual([r["map"] for r in detector.rounds], [1, 1, 1, 2])
        self.assertEqual(len(detector.warnings), 2)

    def test_two_misread_frames_cannot_skip_the_expected_round(self):
        detector = RoundDetector()
        detector.rounds = [{"map": 1, "round": 5, "start": 100}]
        for time, number, timer in [(200, 8, 100), (202, 8, 98), (204, 6, 96), (206, 6, 94)]:
            detector.observe(Observation(time, number, timer, .99))
        self.assertEqual(detector.rounds[-1]["round"], 6)
        self.assertEqual(detector.warnings, [])

    def test_isolated_terminal_map_start_remains_excluded(self):
        detector = RoundDetector()
        detector.rounds = [{"map": 1, "round": 13}, {"map": 2, "round": 1}]
        detector.finalize()
        self.assertTrue(detector.rounds[-1]["excluded"])
        detector.rounds = [{"map": 1, "round": 13}, {"map": 2, "round": 1}, {"map": 2, "round": 2}]
        detector.finalize()
        self.assertFalse(any(item.get("excluded") for item in detector.rounds))


if __name__ == "__main__":
    unittest.main()
