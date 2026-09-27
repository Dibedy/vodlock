import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))
from detector import HudReader, Observation, RoundDetector, parse_hud


class DetectorTests(unittest.TestCase):
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
                                                            [("ROUND8", .93)], []]):
            sample = reader.read(np.zeros((720, 1280, 3), dtype=np.uint8), 3410)
        self.assertEqual((sample.round, sample.timer), (8, 97))

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

    def test_map_reset_and_missing_round_warning(self):
        detector = RoundDetector()
        for number, time in [(12, 100), (14, 400), (1, 1000)]:
            detector.observe(Observation(time, number, 100, .99))
            detector.observe(Observation(time + 2, number, 98, .99))
        self.assertEqual([r["map"] for r in detector.rounds], [1, 1, 2])
        self.assertEqual(len(detector.warnings), 2)


if __name__ == "__main__":
    unittest.main()
