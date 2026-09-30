import hashlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))
from storyboard_align import align_storyboards, extract_storyboard, hamming, translate_index


def unique_hash(value):
    return hashlib.blake2b(str(value).encode(), digest_size=8).hexdigest()


class StoryboardAlignmentTests(unittest.TestCase):
    def test_youtube_storyboard_uses_web_client_with_po_token_provider(self):
        captured = {}

        class Expected(Exception):
            pass

        class Downloader:
            def __init__(self, options):
                captured.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def extract_info(self, *_, **__):
                raise Expected()

        class YtDlp:
            YoutubeDL = Downloader

        with patch.dict(os.environ, {"VODLOCK_YOUTUBE_POT": "1"}):
            with self.assertRaises(Expected):
                extract_storyboard("https://youtube.com/watch?v=example", "youtube", YtDlp)
        self.assertEqual(captured["extractor_args"]["youtube"]["player_client"], ["web"])
        self.assertEqual(captured["extractor_args"]["youtubepot-bgutilhttp"]["base_url"], ["http://127.0.0.1:4416"])

    def test_youtube_storyboard_uses_optional_cookiefile(self):
        captured = {}

        class Expected(Exception):
            pass

        class Downloader:
            def __init__(self, options):
                captured.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def extract_info(self, *_, **__):
                raise Expected()

        class YtDlp:
            YoutubeDL = Downloader

        with patch.dict(os.environ, {"VODLOCK_YOUTUBE_COOKIES": "/tmp/youtube-cookies.txt"}):
            with self.assertRaises(Expected):
                extract_storyboard("https://youtube.com/watch?v=example", "youtube", YtDlp)
        self.assertEqual(captured["cookiefile"], "/tmp/youtube-cookies.txt")

    def test_hamming_distance_counts_changed_bits(self):
        self.assertEqual(hamming("00ff", "01fe"), 2)

    def test_alignment_requires_consistent_anchors_across_full_video(self):
        reference = {"duration": 3000, "interval": 10,
                     "frames": [{"time": index * 10, "hash": unique_hash(index)} for index in range(300)]}
        target = {"duration": 600, "interval": 10,
                  "frames": [{"time": index * 10, "hash": unique_hash(index + 100)} for index in range(60)]}
        result = align_storyboards(reference, target)
        self.assertEqual(result["offset"], 1000)
        self.assertEqual(result["coverage"], [20, 20, 20])
        self.assertEqual(result["anchors"], 60)

    def test_unrelated_storyboards_are_rejected(self):
        reference = {"duration": 600, "interval": 10,
                     "frames": [{"time": index * 10, "hash": unique_hash(index)} for index in range(60)]}
        target = {"duration": 600, "interval": 10,
                  "frames": [{"time": index * 10, "hash": unique_hash(index + 1000)} for index in range(60)]}
        with self.assertRaisesRegex(ValueError, "matching visual anchors"):
            align_storyboards(reference, target, maximum_distance=0)

    def test_alignment_detects_breaks_removed_between_maps(self):
        reference = {"duration": 4000, "interval": 10,
                     "frames": [{"time": index * 10, "hash": unique_hash(index)} for index in range(400)]}
        target_frames = ([{"time": index * 10, "hash": unique_hash(index + 100)} for index in range(60)]
                         + [{"time": (index + 60) * 10, "hash": unique_hash(index + 220)} for index in range(60)])
        result = align_storyboards(reference, {"duration": 1200, "interval": 10, "frames": target_frames})
        self.assertEqual([item["offset"] for item in result["segments"]], [1000.0, 1600.0])
        self.assertEqual(result["anchors"], 120)

    def test_alignment_accepts_small_verified_timebase_drift(self):
        target = {"duration": 600, "interval": 10,
                  "frames": [{"time": index * 10, "hash": unique_hash(index)} for index in range(60)]}
        reference = {"duration": 2000, "interval": 10,
                     "frames": [{"time": 1000 + index * 10.19, "hash": unique_hash(index)}
                                for index in range(60)]}
        result = align_storyboards(reference, target)
        self.assertAlmostEqual(result["timelineScale"], 1.019, places=3)

    def test_short_reference_can_be_located_inside_a_long_watch_party(self):
        reference = {"duration": 600, "interval": 10,
                     "frames": [{"time": index * 10, "hash": unique_hash(index)} for index in range(60)]}
        target = {"duration": 3600, "interval": 10,
                  "frames": [{"time": (index + 120) * 10, "hash": unique_hash(index)} for index in range(60)]
                  + [{"time": index * 10, "hash": unique_hash(index + 1000)} for index in range(300)]}
        result = align_storyboards(reference, target, require_target_coverage=False)
        self.assertEqual(result["offset"], -1200)
        self.assertEqual(result["coverage"], [20, 20, 20])

    def test_index_translation_accounts_for_timeline_scale(self):
        rounds = [{"map": 1, "round": number, "start": number * 100} for number in range(1, 14)]
        translated = translate_index({"rounds": rounds}, {"duration": 2000},
                                     {"offset": 50, "timelineScale": 1.01})
        self.assertEqual(translated[0]["start"], round(50 / 1.01, 2))

    def test_index_translation_selects_and_renumbers_aligned_maps(self):
        rounds = []
        for map_number, base in [(1, 100), (2, 900), (3, 2100), (4, 2900)]:
            rounds.extend({"map": map_number, "round": number, "start": base + number * 40}
                          for number in range(1, 14))
        translated = translate_index({"rounds": rounds}, {"duration": 1500}, {"offset": 2000})
        self.assertEqual(translated[0], {"map": 1, "round": 1, "start": 140.0})
        self.assertEqual(translated[-1]["map"], 2)

    def test_partial_map_alignment_is_rejected(self):
        rounds = [{"map": 2, "round": number, "start": 1000 + number * 40} for number in range(5, 18)]
        with self.assertRaisesRegex(ValueError, "map 1 round 1"):
            translate_index({"rounds": rounds}, {"duration": 1000}, {"offset": 1000})

    def test_disconnect_that_removes_rounds_is_rejected(self):
        rounds = [{"map": 1, "round": number, "start": number * 100} for number in range(1, 14)]
        alignment = {"segments": [
            {"offset": 0, "targetStart": 0, "targetEnd": 450},
            {"offset": 300, "targetStart": 450, "targetEnd": 1000},
        ]}
        with self.assertRaisesRegex(ValueError, "gap"):
            translate_index({"rounds": rounds}, {"duration": 1000}, alignment)


if __name__ == "__main__":
    unittest.main()
