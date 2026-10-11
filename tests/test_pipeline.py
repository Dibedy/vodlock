import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if importlib.util.find_spec("psycopg") is None:
    raise unittest.SkipTest("Install indexer/requirements-worker.txt to run persistent pipeline tests")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))

from detector import HudReader, Observation
from pipeline.cli import release_to_site
from pipeline.config import Config
from pipeline.deployment import Deployment
from pipeline.detection import CandidateDetector, observation_from_dict, redetect_candidates
from pipeline.chat import chat_alignment, compact_chat
from pipeline.errors import NeedsReview, Unsupported, WaitingSource, WaitingWork, failure_kind, retry_after
from pipeline.media import MediaAnalysis, decode_frames, parse_playlist, recognized_teams
from pipeline.publishing import canonical_contract, watchparty_contract
from pipeline.schedule import RiotSchedule, riot_events
from pipeline.storage import LocalStorage
from pipeline.store import Store, backoff
from pipeline.timeline import (
    broadcast_rounds,
    compare_indexes,
    mapped_time,
    piecewise_alignment,
    reconcile_rounds,
    scoreboard_alignment,
    scoreboard_section,
    segment_matches,
    validate_rounds,
)
from pipeline.twitch import resolve_channels, subscribe
from pipeline.youtube import YouTube, metadata_state
from pipeline.worker import Worker, status_server


FIXTURES = Path(__file__).parent / "fixtures" / "pipeline"


def series(teams=("A", "B"), base=100, maps=1, first_map=1, pause=None):
    rounds = []
    for map_number in range(first_map, first_map + maps):
        for number in range(1, 14):
            time = base + (map_number - first_map) * 3000 + (number - 1) * 100
            if pause and number >= pause[0]:
                time += pause[1]
            rounds.append(
                {
                    "map": map_number,
                    "round": number,
                    "start": time,
                    "confidence": 0.99,
                    "scores": [number - 1, 0],
                    "teams": list(teams),
                }
            )
    return rounds


def expected(teams=("A", "B"), order=1, best_of=1):
    return {
        "id": "-".join(teams),
        "team_a": teams[0],
        "team_b": teams[1],
        "match_order": order,
        "best_of": best_of,
        "completion": "completed",
    }


def fingerprints(count=120, base=0, interval=10, offsets=None):
    frames = []
    for index in range(count):
        offset = offsets(index) if offsets else 0
        frames.append(
            {
                "time": base + index * interval + offset,
                "hash": hashlib.blake2b(str(index).encode(), digest_size=8).hexdigest(),
            }
        )
    return {"frames": frames, "duration": frames[-1]["time"] + interval, "interval": interval}


class PipelineTests(unittest.TestCase):
    def test_adaptive_archive_ocr_preserves_rounds_with_fewer_analyses(self):
        detected = []
        counts = []
        for adaptive in (False, True):
            media = MediaAnalysis(decoder=Mock(return_value=((second, None) for second in range(240) if second != 29)))
            def observe(frame, timestamp, *args, **kwargs):
                start = 28 if timestamp < 148 else 148
                number = 1 if timestamp < 148 else 2
                if start <= timestamp < start + 40:
                    return Observation(timestamp, number, 100 - int(timestamp - start), .99, scores=(0, number - 1)), {}
                return Observation(timestamp, None, None, 0), {}
            media.observation = Mock(side_effect=observe)
            detector = CandidateDetector()
            rounds = []
            for sample, extra, frame in media.window({'url': 'fixture'}, 0, 240, adaptive=adaptive):
                candidate = detector.observe(sample, extra=extra)
                if candidate['accepted']:
                    rounds.append((candidate['round_number'], candidate['evidence']['start']))
            detected.append(rounds)
            counts.append(media.observation.call_count)
        self.assertEqual(detected, [[(1, 28), (2, 148)]] * 2)
        self.assertLess(counts[1], counts[0] * .6)

    def test_break_scan_rejects_highlights_and_recovers_new_round_start(self):
        media = MediaAnalysis(decoder=lambda *args, **kwargs: ((second, second) for second in range(200)))
        detector = CandidateDetector({"previous": {"round": 16, "start": 0, "scores": [12, 3]}})
        def observe(frame, timestamp, *args, **kwargs):
            for start, number, teams in [(80, 1, ["A", "B"]), (121, 1, ["C", "D"]), (150, 2, ["C", "D"])]:
                if start <= timestamp < start + 17:
                    return Observation(timestamp, number, 100 - int(timestamp - start), .99,
                                       scores=(number - 1, 0)), {"teams": teams}
            return Observation(timestamp, None, None), {}
        media.observation = Mock(side_effect=observe)
        media.fingerprint = Mock(side_effect=lambda image, **kwargs: {
            "gameplayHash": f"h{image - 70}" if 80 <= image <= 96 else f"new{image}"})
        history = [{"time": second, "gameplayHash": f"h{second}"} for second in range(10, 28, 2)]
        accepted = []
        samples = []
        for sample, extra, frame in media.window({"url": "fixture"}, 0, 200, adaptive=True,
                                                detector=detector, replay_frames=history):
            candidate = detector.observe(sample, extra=extra)
            samples.append({**candidate, "timeline": "archive", "id": str(sample.time)})
            if candidate["accepted"]:
                accepted.append((candidate["round_number"], candidate["evidence"]["start"]))
        self.assertEqual(accepted, [(1, 121), (2, 150)])
        redetected = redetect_candidates(samples)[0]
        self.assertEqual([(value["round_number"], value["evidence"]["start"]) for value in redetected], accepted)
        self.assertLess(media.observation.call_count, 120)

    def test_break_hud_change_wakes_scan_before_next_scheduled_probe(self):
        import numpy as np
        media = MediaAnalysis()
        detector = CandidateDetector({"scan": {"mode": "break", "next_probe": 6}})
        def observe(frame, timestamp, *args, **kwargs):
            if 2 <= timestamp <= 5:
                return Observation(timestamp, 1, 102 - timestamp, .99, scores=(0, 0)), {"teams": ["A", "B"]}
            return Observation(timestamp, None, None), {}
        media.observation = Mock(side_effect=observe)
        def frames():
            for second in range(12):
                frame = np.zeros((720, 1280, 3), dtype=np.uint8)
                if 2 <= second <= 5:
                    frame[25:60, 610:620] = 255
                    frame[25:60, 645:655] = 255
                yield second, frame
        accepted = []
        for sample, extra, frame in media.scan_frames(frames(), detector=detector):
            candidate = detector.observe(sample, extra=extra)
            if candidate["accepted"]:
                accepted.append(candidate["evidence"]["start"])
        self.assertEqual(accepted, [2])

    def test_break_animation_does_not_wake_ocr_without_clock_appearance(self):
        import numpy as np
        media = MediaAnalysis()
        detector = CandidateDetector({"scan": {"mode": "break", "next_probe": 6, "quiet_since": 0}})
        media.observation = Mock(side_effect=lambda frame, timestamp, *args, **kwargs:
                                 (Observation(timestamp, None, None), {}))
        frames = ((second, np.full((720, 1280, 3), (second * 19) % 255, dtype=np.uint8)) for second in range(60))
        list(media.scan_frames(frames, detector=detector))
        self.assertLessEqual(media.observation.call_count, 15)

    def test_frozen_clock_does_not_keep_dense_scan_active(self):
        media = MediaAnalysis(decoder=lambda *args, **kwargs: ((second, None) for second in range(180)))
        media.observation = Mock(side_effect=lambda frame, timestamp, *args, **kwargs:
                                 (Observation(timestamp, 1, 100, .99, scores=(0, 0)), {}))
        detector = CandidateDetector()
        accepted = []
        for sample, extra, frame in media.window({"url": "fixture"}, 0, 180, adaptive=True, detector=detector):
            if detector.observe(sample, extra=extra)["accepted"]:
                accepted.append(sample.time)
        self.assertEqual(accepted, [])
        self.assertLess(media.observation.call_count, 65)

    def test_confirmed_round_uses_probe_without_repeating_full_hud_ocr(self):
        reader = Mock()
        reader.read_clock.return_value = Observation(105, None, 95, .99)
        reader.read_scoreboard.return_value = Observation(105, 8, 95, .99, scores=(7, 0))
        media = MediaAnalysis(reader=reader)
        sample, extra = media.observation(None, 105, dense=False, previous={"round": 8, "scores": [7, 0]})
        self.assertEqual(sample.round, 8)
        self.assertTrue(extra["probe_only"])
        reader.read.assert_not_called()
        detector = CandidateDetector()
        candidates = []
        for second in range(3):
            candidate = detector.observe(Observation(105 + second, 8, 95 - second, .99, scores=(7, 0)), extra=extra)
            candidates.append({**candidate, "timeline": "archive", "id": str(second)})
        self.assertEqual(redetect_candidates(candidates)[0], [])

    def test_archive_decoder_reuses_contiguous_windows_and_closes_on_seek(self):
        closed = []
        def decode(media, duration, start=0, interval=1, **kwargs):
            try:
                yield from ((offset, start + offset) for offset in range(0, int(duration), interval))
            finally:
                closed.append(start)
        decoder = Mock(side_effect=decode)
        media = MediaAnalysis(decoder=decoder)
        self.addCleanup(media.close)
        remote = {"url": "fixture", "duration": 1000}
        self.assertEqual([position for position, _ in media.archive_frames(remote, 0, 90)], list(range(90)))
        self.assertEqual([position for position, _ in media.archive_frames(remote, 90, 180)], list(range(90, 180)))
        self.assertEqual(decoder.call_count, 1)
        self.assertEqual([position for position, _ in media.archive_frames(remote, 300, 390)], list(range(300, 390)))
        self.assertEqual(decoder.call_count, 2)
        self.assertIn(0, closed)
        iterator = media.archive_frames(remote, 390, 480)
        next(iterator)
        iterator.close()
        self.assertIn(300, closed)
        self.assertEqual(media.stream_cache, {})

    def test_archive_decoder_expiry_boundaries_and_cross_slot_reuse_are_safe(self):
        decoder = Mock(side_effect=lambda media, duration, start=0, interval=1, **kwargs:
                       ((offset, None) for offset in range(0, int(duration), interval)))
        media = MediaAnalysis(decoder=decoder)
        other = MediaAnalysis(decoder=decoder)
        other.stream_cache, other.stream_lock = media.stream_cache, media.stream_lock
        self.addCleanup(media.close)
        remote = {"url": "fixture", "duration": 700}
        positions = []
        for start in range(0, 630, 90):
            positions.extend(position for position, _ in (other if start % 180 else media).archive_frames(remote, start, start + 90))
        self.assertEqual(positions, list(range(630)))
        self.assertEqual(decoder.call_count, 2)
        for entry in media.stream_cache.values():
            entry["expires"] = -1
        list(media.archive_frames(remote, 630, 700))
        self.assertEqual(decoder.call_count, 3)
        self.assertEqual(media.stream_cache, {})

    def test_archive_decoder_incomplete_range_cannot_advance_checkpoint(self):
        for interval, missing in [(1, 89), (2, 88)]:
            with self.subTest(interval=interval):
                media = MediaAnalysis(decoder=lambda *args, **kwargs: iter((offset, None) for offset in range(0, missing, interval)))
                self.addCleanup(media.close)
                with self.assertRaises(WaitingSource):
                    list(media.archive_frames({"url": "fixture", "duration": 90}, 0, 90, interval=interval))
                self.assertEqual(media.stream_cache, {})

    def test_alternating_recovery_jobs_reuse_their_own_bounded_decoders(self):
        decoder = Mock(side_effect=lambda media, duration, start=0, interval=1, **kwargs:
                       ((offset, None) for offset in range(0, int(duration), interval)))
        media = MediaAnalysis(decoder=decoder)
        other = MediaAnalysis(decoder=decoder)
        other.stream_cache, other.stream_lock = media.stream_cache, media.stream_lock
        self.addCleanup(media.close)
        remote = {"url": "fixture", "duration": 1500}
        for stream, start in [("tail", 0), ("gap", 600), ("tail", 90), ("gap", 690)]:
            positions = [position for position, _ in other.archive_frames(remote, start, start + 90, stream_id=stream)]
            self.assertEqual(positions, list(range(start, start + 90)))
            self.assertLessEqual(len(media.stream_cache), 2)
        self.assertEqual(decoder.call_count, 2)
        list(media.archive_frames(remote, 1200, 1290, stream_id="third"))
        self.assertEqual(len(media.stream_cache), 2)

    def test_video_chunks_share_downloads_across_modes_urls_and_worker_slots(self):
        decoder = Mock(side_effect=lambda path, duration, start=0, interval=1, **kwargs:
                       ((second * interval, None) for second in range(math.ceil(duration / interval))))
        media = MediaAnalysis(decoder=decoder)
        other = MediaAnalysis(decoder=decoder)
        other.video_cache, other.video_lock = media.video_cache, media.video_lock
        self.addCleanup(media.close)
        media.download_clip = Mock(side_effect=lambda remote, lower, upper, path: path.write_bytes(b"fixture"))
        other.download_clip = media.download_clip
        remote = {"url": "first-url", "duration": 600, "cache_identity": ("youtube", "fixture", "720"), "cache_revision": (1, 1)}
        self.assertEqual([second for second, _ in media.cached_archive_frames(remote, 4, start=10)], list(range(4)))
        self.assertEqual([second for second, _ in other.cached_archive_frames({**remote, "url": "refreshed-url"}, 4, start=10, interval=2)], [0, 2])
        self.assertEqual(media.download_clip.call_count, 1)
        self.assertEqual(media.download_clip.call_args.args[1:3], (10, 14))
        self.assertTrue(all(call.kwargs["absolute"] for call in decoder.call_args_list))
        self.assertEqual([second for second, _ in media.cached_archive_frames(remote, 6, start=12)], list(range(6)))
        self.assertEqual(media.download_clip.call_count, 2)
        self.assertEqual(media.download_clip.call_args.args[1:3], (14, 18))
        list(media.cached_archive_frames({**remote, "cache_revision": (1, 2)}, 4, start=10))
        list(media.cached_archive_frames({**remote, "cache_identity": ("youtube", "fixture", "540")}, 4, start=10))
        self.assertEqual(media.download_clip.call_count, 4)

    def test_video_chunk_cache_is_bounded_expires_and_discards_failed_reads(self):
        media = MediaAnalysis(decoder=lambda path, duration, **kwargs: ((second, None) for second in range(math.ceil(duration))))
        self.addCleanup(media.close)
        media.download_clip = Mock(side_effect=lambda remote, lower, upper, path: path.write_bytes(b"fixture"))
        remote = {"url": "fixture", "duration": 1000, "cache_identity": ("source",)}
        paths = []
        for start in range(0, 600, 120):
            list(media.cached_archive_frames(remote, 2, start=start))
            paths.append(media.download_clip.call_args.args[3])
            self.assertLessEqual(len(media.video_cache), 4)
        self.assertFalse(paths[0].exists())
        for entry in media.video_cache.values():
            entry["expires"] = -1
        list(media.cached_archive_frames(remote, 2, start=480))
        self.assertEqual(len(media.video_cache), 1)
        self.assertTrue(all(not path.exists() for path in paths))
        media.decoder = lambda *args, **kwargs: iter([(0, None)])
        with self.assertRaisesRegex(WaitingSource, "ended before"):
            list(media.cached_archive_frames(remote, 2, start=480))
        self.assertEqual(media.video_cache, {})
        media.download_clip.side_effect = WaitingSource("Failed download")
        with self.assertRaisesRegex(WaitingSource, "Failed download"):
            list(media.cached_archive_frames(remote, 2))
        self.assertEqual(media.video_cache, {})
        self.assertFalse(media.download_clip.call_args.args[3].exists())

    def test_adaptive_first_pass_streams_directly_but_reuses_available_video(self):
        decoder = Mock(side_effect=lambda path, duration, **kwargs: ((second, None) for second in range(math.ceil(duration))))
        media = MediaAnalysis(decoder=decoder)
        self.addCleanup(media.close)
        media.download_clip = Mock(side_effect=lambda remote, lower, upper, path: path.write_bytes(b"fixture"))
        media.scan_frames = Mock(side_effect=lambda frames, *args: ((Observation(second, None, None), {}, frame) for second, frame in frames))
        remote = {"url": "fixture", "duration": 600, "cache_identity": ("source",), "cache_revision": (1, 1)}
        self.assertEqual([sample.time for sample, _, _ in media.window(remote, 0, 4, adaptive=True)], list(range(4)))
        media.download_clip.assert_not_called()
        self.assertEqual(decoder.call_args.args[0], "fixture")
        media.close()
        list(media.cached_archive_frames(remote, 4))
        decoder.reset_mock()
        self.assertEqual([sample.time for sample, _, _ in media.window(remote, 0, 4, adaptive=True)], list(range(4)))
        self.assertTrue(decoder.call_args.kwargs["absolute"])
        self.assertEqual(media.download_clip.call_count, 1)

    def test_concurrent_video_chunk_readers_share_one_completed_download(self):
        media = MediaAnalysis(decoder=lambda path, duration, **kwargs: ((second, None) for second in range(math.ceil(duration))))
        self.addCleanup(media.close)
        entered, release = threading.Event(), threading.Event()
        def download(remote, lower, upper, path):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test download was not released")
            path.write_bytes(b"fixture")
        media.download_clip = Mock(side_effect=download)
        results, errors = [], []
        remote = {"url": "fixture", "duration": 120, "cache_identity": ("source",)}
        def read():
            try:
                results.append(list(media.cached_archive_frames(remote, 3)))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=read) for _ in range(2)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(5))
        release.set()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [[(0, None), (1, None), (2, None)]] * 2)
        self.assertEqual(media.download_clip.call_count, 1)
        self.assertTrue(all(entry["references"] == 0 for entry in media.video_cache.values()))

    def test_clip_packet_verification_rejects_gaps_bad_timebases_and_short_downloads(self):
        media = MediaAnalysis()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.nut"
            path.write_bytes(b"fixture")
            for payload in [b"invalid", b"#tb 0: 1/0\n0,0,0,1,10,0\n", b"#tb 0: 1/1\n0,0,0,1,10,0\n",
                            b"#tb 0: 1/1\n0,0,0,1,10,0\n0,3,3,1,10,0\n"]:
                with self.subTest(payload=payload):
                    with patch("pipeline.media.subprocess.run", side_effect=[Mock(returncode=0), Mock(returncode=0, stdout=payload)]):
                        with self.assertRaises(WaitingSource):
                            media.download_clip({"url": "fixture", "duration": 4}, 0, 4, path)

    def test_real_video_cache_preserves_fractional_seeks_b_frames_and_chunk_boundaries(self):
        import imageio_ffmpeg
        import numpy as np
        import cv2
        with tempfile.TemporaryDirectory() as directory:
            raw, source, shifted = (Path(directory) / name for name in ("frames.avi", "source.mp4", "shifted.ts"))
            writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"FFV1"), 10, (160, 90))
            self.assertTrue(writer.isOpened())
            for number in range(1250):
                writer.write(np.full((90, 160, 3), number % 255, np.uint8))
            writer.release()
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-i", str(raw), "-c:v", "libx264",
                            "-crf", "18", "-g", "50", "-bf", "2", str(source)], check=True, capture_output=True)
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-i", str(source), "-c:v", "copy",
                            "-output_ts_offset", "2150.123", "-mpegts_copyts", "1", str(shifted)], check=True, capture_output=True)
            for path in (source, shifted):
                with self.subTest(path=path.name):
                    media = MediaAnalysis()
                    self.addCleanup(media.close)
                    remote = {"url": str(path), "duration": 125, "cache_identity": (path.name,)}
                    for start, end, interval, compact in [(2.25, 6.25, 1, False), (3.012, 7.012, 1, True),
                                                         (117.25, 125, 1, False), (119.25, 125, 2, False), (0, 125, 1, False)]:
                        expected = list(decode_frames(source, end - start, start=start, interval=interval, compact=compact))
                        actual = list(media.archive_frames(remote, start, end, interval=interval, compact=compact))
                        self.assertEqual(len(actual), len(expected))
                        for (position, image), (offset, original) in zip(actual, expected):
                            self.assertAlmostEqual(position, start + offset, places=6)
                            self.assertTrue(np.array_equal(image, original), f"start={start} interval={interval} compact={compact} actual={image.mean()} expected={original.mean()}")

    def test_invalid_timestamps_are_blocking_validation_findings(self):
        for timestamp in (float('nan'), float('inf'), -1, True, '100'):
            with self.subTest(timestamp=timestamp):
                values = series()
                values[3]['start'] = timestamp
                self.assertIn('invalid_timestamp', {finding['code'] for finding in validate_rounds(values, 1)})
                values[0]['map'] = 2
                values[0]['start'] = timestamp
                self.assertIn('invalid_timestamp', {finding['code'] for finding in validate_rounds(values, 1)})

    def test_youtube_metadata_preserves_channel_identity_for_publication_approval(self):
        with patch.dict(os.environ, {"YOUTUBE_API_KEY": ""}):
            with patch("yt_dlp.YoutubeDL") as factory:
                factory.return_value.__enter__.return_value.extract_info.return_value = {
                    "id": "abcdefghijk", "title": "Official broadcast", "channel_id": "official",
                    "live_status": "is_live", "release_timestamp": 1791620991}
                value = YouTube().metadata("abcdefghijk")
        self.assertEqual(value["metadata"]["channel_id"], "official")

    def test_watchparty_reader_checks_round_label_against_repeated_score_digits(self):
        import numpy as np

        reader = HudReader.__new__(HudReader)
        reader.read_clock_lines = Mock(return_value=[("1:40", .99)])
        reader.read_lines = Mock(side_effect=[[('ROUND 8', .99)], [('7', .8)], [('0', .8)], []])
        sample = reader.read_scoreboard(np.zeros((720, 1280, 3), dtype=np.uint8), 100)
        self.assertEqual((sample.round, sample.timer, sample.scores), (8, 100, (7, 0)))
        reader.read_lines = Mock(side_effect=[[('ROUND 8', .99)], [('7', .8)], [('1', .8)]] * 3)
        self.assertIsNone(reader.read_scoreboard(np.zeros((720, 1280, 3), dtype=np.uint8), 100).round)

    def test_watchparty_windows_fetch_only_local_segments_and_seek_accurately(self):
        import numpy as np

        segments = [{"duration": 10, "url": f"https://video/{number}", "group": 0, "init": None}
                    for number in range(20)]
        reader = Mock()
        reader.read_scoreboard.return_value = Observation(45, 1, 100)
        decoder = Mock(return_value=iter([(0, np.zeros((720, 1280, 3), dtype=np.uint8))]))
        requester = Mock(return_value=b"video")
        media = MediaAnalysis(requester=requester, decoder=decoder, reader=reader)
        remote = {"url": "https://video/playlist", "vod_segments": segments}
        values = list(media.watchparty_window(remote, 45, 53))
        self.assertEqual(len(values), 1)
        self.assertEqual(decoder.call_args.kwargs, {"start": 25.0, "compact": False, "accurate": True})
        self.assertEqual({call.args[0] for call in requester.call_args_list}, {f"https://video/{number}" for number in range(2, 6)})
        decoder.return_value = iter([])
        list(media.watchparty_window(remote, 45, 53))
        self.assertEqual(requester.call_count, 4)
        segments[4]["group"] = 1
        list(media.watchparty_window(remote, 45, 53))
        self.assertEqual([call.kwargs["start"] for call in decoder.call_args_list[-2:]], [5, 0])

    def test_large_watchparty_window_streams_bounded_chunks_and_closes_early(self):
        import numpy as np

        segments = [{"duration": 10, "url": f"https://video/{number}", "group": 0, "init": None} for number in range(30)]
        requester = Mock(return_value=b"x" * (4 * 1024 * 1024))
        closed = []
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        def decode(path, duration, **kwargs):
            try:
                self.assertLessEqual(duration, 25)
                for second in range(int(duration)):
                    yield second, frame
            finally:
                closed.append(duration)
        reader = Mock()
        reader.read_scoreboard.side_effect = lambda image, timestamp: Observation(timestamp, None, None)
        remote = {"vod_segments": segments}
        media = MediaAnalysis(requester=requester, decoder=decode, reader=reader)
        stream = media.watchparty_window(remote, 20, 200)
        next(stream)
        self.assertLessEqual(requester.call_count, 5)
        stream.close()
        self.assertEqual(closed, [25])
        requester.reset_mock()
        values = list(media.watchparty_window(remote, 20, 200))
        self.assertEqual([value[0].time for value in values], list(range(20, 200)))
        self.assertLessEqual(len(remote["vod_segment_cache"]), 12)
        self.assertLessEqual(sum(map(len, remote["vod_segment_cache"].values())), 64 * 1024 * 1024)

    def test_decoder_stall_kills_process_instead_of_renewing_a_stuck_job(self):
        release = threading.Event()
        process = Mock()
        process.stdout.read.side_effect = lambda size: (release.wait(4), b"")[1]
        process.kill.side_effect = release.set
        process.poll.return_value = 0
        ticks = iter([0, 100, 100])
        with patch("pipeline.media.subprocess.Popen", return_value=process), patch("pipeline.media.monotonic", side_effect=lambda: next(ticks, 100)):
            with self.assertRaisesRegex(WaitingSource, "decoder stalled"):
                list(decode_frames("fixture", 10))
        process.kill.assert_called_once()
        process.stdout.close.assert_called_once()

    def test_failure_classification_and_provider_retry_after(self):
        self.assertEqual(failure_kind(TimeoutError("timeout")), "timeout")
        self.assertEqual(failure_kind(WaitingWork("dependency")), "dependency")
        self.assertEqual(failure_kind(NeedsReview("uncertain rounds")), "data_quality")
        self.assertEqual(failure_kind(WaitingSource("TWITCH_DOWNLOADER is required")), "configuration")
        self.assertEqual(failure_kind(HTTPError("https://provider", 429, "limited", {"Retry-After": "900"}, None)), "rate_limit")
        self.assertEqual(retry_after(HTTPError("https://provider", 429, "limited", {"Retry-After": "900"}, None)), 900)
        self.assertEqual(retry_after(HTTPError("https://provider", 503, "down", {"Retry-After": "Wed, 01 Jan 2100 00:00:00 GMT"}, None)), 86400)
        self.assertIsNone(retry_after(HTTPError("https://provider", 429, "limited", {"Retry-After": "invalid"}, None)))
        with patch("pipeline.store.random.uniform", return_value=1.2):
            self.assertEqual(backoff(0, jitter=True), 36)
            self.assertEqual(backoff(100, jitter=True), 3600)

    def test_local_alignment_does_not_repeat_identical_offset_groups(self):
        import statistics
        from pipeline.timeline import local_alignment

        matches = [({"time": number * 2}, {"time": number * 2 + 100}, 0) for number in range(1000)]
        with patch("pipeline.timeline.matching_frames", return_value=iter(matches)), patch("pipeline.timeline.statistics.median", wraps=statistics.median) as median:
            result = local_alignment({"frames": [{}, {}], "interval": 2}, {"frames": [], "interval": 2})
        self.assertEqual(median.call_count, 2)
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0], {"sourceStart": 0, "sourceEnd": 1998, "canonicalStart": 100,
                                              "canonicalEnd": 2098, "offset": 100, "anchors": 1000, "maximumResidual": 0})

    def test_supervisor_restarts_crashes_but_respects_clean_shutdown_and_retry_limit(self):
        from pipeline.worker import supervise

        with patch.dict(os.environ, {"SPOILLESS_SUPERVISOR_STOP": ""}), patch("pipeline.worker.backoff", return_value=0):
            with patch("pipeline.worker.subprocess.run", side_effect=[Mock(returncode=1), Mock(returncode=1), Mock(returncode=0)]) as run:
                supervise("fixture.json")
                self.assertEqual(run.call_count, 3)
                self.assertEqual(run.call_args.kwargs["env"]["SPOILLESS_SUPERVISOR_PARENT_PID"], str(os.getppid()))
                self.assertIs(run.call_args.kwargs["stderr"], sys.stderr)
            with patch("pipeline.worker.subprocess.run", return_value=Mock(returncode=1)) as run:
                with self.assertRaisesRegex(RuntimeError, "eight attempts"):
                    supervise("fixture.json")
                self.assertEqual(run.call_count, 8)

    def test_scoreboard_verification_requires_countdown_scores_and_visible_lead(self):
        value = {"round": 8, "scores": [7, 0]}
        samples = [Observation(200 + second, 8, 100 - second if second >= 0 else 0, .99,
                               scores=(0, 7), buy_phase=second < 0) for second in range(-7, 7)]
        section = scoreboard_section(samples, value, 100, 1)
        self.assertEqual(section["offset"], -100)
        self.assertGreaterEqual(section["anchors"], 5)
        self.assertIsNone(scoreboard_section(samples, {"round": 8, "scores": [6, 1]}, 100, 1))
        self.assertIsNone(scoreboard_section(samples[7:], value, 100, 1))
        for sample in samples:
            sample.replay = True
        self.assertIsNone(scoreboard_section(samples, value, 100, 1))

    def test_scoreboard_verification_rejects_frozen_stats_and_low_confidence(self):
        samples = [Observation(200 + second, 8, 98, .99, scores=(7, 0)) for second in range(-7, 7)]
        self.assertIsNone(scoreboard_section(samples, {"round": 8}, 100, 1))
        for second, sample in enumerate(samples):
            sample.timer = 100 - second
            sample.confidence = .6
        self.assertIsNone(scoreboard_section(samples, {"round": 8}, 100, 1))

    def test_scoreboard_alignment_repairs_local_gap_without_extending_coverage(self):
        original = {"timelineScale": 1, "segments": [
            {"sourceStart": 100, "sourceEnd": 180, "canonicalStart": 0, "canonicalEnd": 80,
             "offset": -100, "anchors": 10, "maximumResidual": 1},
            {"sourceStart": 220, "sourceEnd": 300, "canonicalStart": 120, "canonicalEnd": 200,
             "offset": -100, "anchors": 10, "maximumResidual": 1}]}
        samples = [Observation(208 + second, 8, 100 - second if second >= 0 else 0, .99,
                               scores=(7, 0), buy_phase=second < 0) for second in range(-7, 7)]
        section = scoreboard_section(samples, {"round": 8}, 100, 1)
        result = scoreboard_alignment(original, {"match:1:8": {"section": section}})
        self.assertEqual(len(result["segments"]), 3)
        self.assertEqual(result["segments"][1]["offset"], -108)
        self.assertEqual(original["segments"][0]["sourceEnd"], 180)
        index = {"id": "index", "broadcast_id": "broadcast", "version": 1, "state": "final", "archive_revision": 1,
                 "rounds": [{"map": 1, "round": 8, "start": 100}]}
        contract = watchparty_contract(
            {"provider": "youtube", "role": "canonical", "broadcast_id": "broadcast", "external_id": "abcdefghijk"},
            {"provider": "twitch", "role": "watch_party", "revision": 1, "metadata": {"vod_id": "123456789"}},
            expected(), index, result, 1)
        self.assertEqual(contract["rounds"], [{"map": 1, "round": 8, "start": 208}])
        with self.assertRaises(NeedsReview):
            mapped_time(195, result)

    def test_scoreboard_alignment_splits_old_sections_and_rejects_conflicting_checks(self):
        mapping = {"timelineScale": 1, "segments": [
            {"sourceStart": 0, "sourceEnd": 400, "canonicalStart": 100, "canonicalEnd": 500,
             "offset": 100, "anchors": 50, "maximumResidual": 1}]}
        samples = [Observation(200 + second, 8, 100 - second if second >= 0 else 0, .99,
                               scores=(7, 0), buy_phase=second < 0) for second in range(-7, 7)]
        section = scoreboard_section(samples, {"round": 8}, 310, 1)
        result = scoreboard_alignment(mapping, {"a": {"section": section}})
        self.assertEqual(len(result["segments"]), 3)
        for left, right in zip(result["segments"], result["segments"][1:]):
            self.assertLessEqual(left["sourceEnd"], right["sourceStart"])
            self.assertLessEqual(left["canonicalEnd"], right["canonicalStart"])
        with self.assertRaises(NeedsReview):
            scoreboard_alignment(mapping, {"a": {"section": section}, "b": {"section": section}})

    def test_watchparty_piecewise_rounds_keep_canonical_provenance_and_five_second_lead(self):
        canonical = {"provider": "youtube", "role": "canonical", "broadcast_id": "broadcast", "external_id": "abcdefghijk"}
        secondary = {"provider": "twitch", "role": "watch_party", "revision": 1, "metadata": {"vod_id": "123456789"}}
        index = {"id": "index", "broadcast_id": "broadcast", "version": 3, "state": "final", "archive_revision": 1, "rounds": series(maps=2)}
        mapping = {"timelineScale": 1, "segments": [
            {"sourceStart": 100, "sourceEnd": 1500, "canonicalStart": 0, "canonicalEnd": 1400, "offset": -100, "anchors": 12, "maximumResidual": .1},
            {"sourceStart": 3200, "sourceEnd": 4600, "canonicalStart": 3000, "canonicalEnd": 4400, "offset": -200, "anchors": 12, "maximumResidual": .1}]}
        contract = watchparty_contract(canonical, secondary, expected(), index, mapping, 2)
        self.assertEqual(contract["rounds"][0]["start"], 200)
        self.assertEqual(contract["rounds"][13]["start"], 3300)
        self.assertEqual(contract["canonical"]["rounds"][0]["start"], 100)
        self.assertEqual(contract["leadSeconds"], 5)
        self.assertTrue(contract["alignment"]["strictCoverage"])
        shifted = {**index, "rounds": [{**value, "start": value["start"] + 7.793} for value in index["rounds"]]}
        self.assertEqual(watchparty_contract(canonical, secondary, expected(), shifted, mapping, 2, 7.793)["rounds"], contract["rounds"])
        mapping["segments"][0]["canonicalEnd"] = 101
        with self.assertRaises(NeedsReview):
            watchparty_contract(canonical, secondary, expected(), index, mapping, 2)

    def test_auto_approval_requires_verified_channel_event_and_date(self):
        rule = {"channel_id": "official", "event": "Champions", "from_day": "2026-10-08", "through_day": "2026-10-31"}
        config = Config("test", shadow=False, settings={"auto_publish": [rule]})
        broadcast = {"youtube_id": "new", "channel_id": "official", "event": "Champions", "day": "2026-10-09",
                     "metadata": {"channel_id": "official"}}
        self.assertFalse(config.shadow_for(broadcast))
        for changes in ({"channel_id": "other"}, {"event": "other"}, {"day": "2026-10-07"},
                        {"day": "2026-11-01"}, {"metadata": {}}, {"metadata": {"channel_id": "other"}}):
            self.assertTrue(config.shadow_for({**broadcast, **changes}))
        self.assertTrue(Config("test", settings=config.settings).shadow_for(broadcast))

    def test_auto_approval_configuration_rejects_unconfigured_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.json"
            settings = {"youtube_channels": [{"channel_id": "official", "event": "Champions"}],
                        "auto_publish": [{"channel_id": "official", "event": "Champions", "from_day": "2026-10-08"}]}
            with patch.dict(os.environ, {"DATABASE_URL": "test", "SPOILLESS_PIPELINE_MODE": "publish"}):
                path.write_text(json.dumps(settings))
                self.assertFalse(Config.load(path).shadow)
                settings["auto_publish"][0]["channel_id"] = "untrusted"
                path.write_text(json.dumps(settings))
                with self.assertRaises(ValueError):
                    Config.load(path)

    def test_shadow_worker_never_queues_deployment(self):
        store = Mock()
        worker = Worker(store, Config("test", settings={"deployment": {"repository": "https://github.com/a/b.git"}}))
        with patch("pipeline.worker.export_snapshot") as export:
            worker.export({})
        export.assert_called_once()
        store.enqueue.assert_not_called()

    def test_deployment_git_errors_do_not_expose_credentials(self):
        deploy = Deployment(Mock(), Config("test"))
        with patch.dict(os.environ, {"SPOILLESS_GITHUB_TOKEN": "private-token"}), patch("pipeline.deployment.subprocess.run") as run:
            run.return_value = Mock(returncode=1, stderr="private-token", stdout="")
            with self.assertRaises(WaitingSource) as error:
                deploy.git(Path("."), "push", "origin", "main")
            self.assertNotIn("private-token", str(error.exception))
            self.assertNotIn("private-token", str(run.call_args.args))

    def test_deployment_budget_prioritizes_readiness_and_reserves_capacity(self):
        connection = Mock()
        settings = {"deployment": {"repository": "https://github.com/a/b.git", "branch": "main"}}
        deploy = Deployment(Mock(), Config("test", shadow=False, settings=settings))
        for usage in ({"total": 3, "incremental": 1, "elapsed": 120},
                      {"total": 12, "incremental": 1, "elapsed": 21600},
                      {"total": 5, "incremental": 4, "elapsed": 21600}):
            connection.execute.return_value.fetchone.return_value = usage
            deploy.check_budget(connection, settings["deployment"], "ready")
            with self.assertRaises(WaitingWork):
                deploy.check_budget(connection, settings["deployment"], "incremental")
        for elapsed in (None, 21600):
            connection.execute.return_value.fetchone.return_value = {"total": 0, "incremental": 0, "elapsed": elapsed}
            deploy.check_budget(connection, settings["deployment"], "incremental")
        connection.execute.return_value.fetchone.return_value = {"total": 20, "incremental": 4, "elapsed": 21600}
        for kind in ("ready", "incremental"):
            with self.assertRaises(WaitingWork):
                deploy.check_budget(connection, settings["deployment"], kind)

    def test_deployment_distinguishes_ready_matches_and_chat_from_incremental_alignment(self):
        deploy = Deployment(Mock(), Config("test"))
        entry = {"canonicalPipeline": True, "catalogId": "match", "expectedMatchId": "match",
                 "pipelineState": "provisional", "pipelineVersion": 1, "index": "/indexes/old.json"}
        previous = {"videos": [entry]}
        index = {"rounds": [{"map": 1, "round": 1, "start": 100}], "pipelineState": "provisional",
                 "alignment": {"segments": [{"targetStart": 0, "targetEnd": 200}]}}
        before = {entry["index"]: index}
        current_entry = {**entry, "index": "/indexes/new.json"}
        current = {"videos": [current_entry]}
        updated = {**index, "alignment": {"segments": [{"targetStart": 0, "targetEnd": 300}]}}
        after = {current_entry["index"]: updated}
        self.assertEqual(deploy.publication_kind(previous, current, before, after, set()), "incremental")
        legacy = {"title": "B vs A", "index": "/indexes/legacy.json"}
        self.assertEqual(deploy.publication_kind({"videos": [entry, legacy]}, current, before, after, set()), "ready")
        self.assertEqual(deploy.publication_kind({"videos": [entry, legacy]}, {"videos": [current_entry, legacy]}, before, after, set()), "incremental")
        self.assertEqual(deploy.publication_kind({"videos": []}, current, {}, after, set()), "ready")
        self.assertEqual(deploy.publication_kind(previous, {"videos": []}, before, {}, set()), "ready")
        self.assertEqual(deploy.publication_kind(previous, {"videos": [{**current_entry, "pipelineState": "final"}]}, before, after, set()), "ready")
        after[current_entry["index"]] = {**updated, "rounds": [{"map": 1, "round": 1, "start": 101}]}
        self.assertEqual(deploy.publication_kind(previous, current, before, after, set()), "ready")
        after[current_entry["index"]] = updated
        current_entry.update(chat="/chats/stream.json", chatSourceId="stream")
        after[current_entry["chat"]] = {"messages": [{"t": 100}]}
        self.assertEqual(deploy.publication_kind(previous, current, before, after, set()), "ready")
        before[entry["index"]] = {**index, "alignment": {"segments": [{"targetStart": 400, "targetEnd": 500}]}}
        entry.update(chat=current_entry["chat"], chatSourceId="stream")
        before[entry["chat"]] = after[current_entry["chat"]]
        self.assertEqual(deploy.publication_kind(previous, current, before, after, set()), "ready")
        before[entry["index"]] = index
        self.assertEqual(deploy.publication_kind(previous, current, before, after, set()), "incremental")
        self.assertEqual(deploy.publication_kind(previous, current, before, after, {"stream"}), "ready")
        after[current_entry["index"]] = index
        self.assertEqual(deploy.publication_kind(previous, current, before, after, {"stream"}), "incremental")

    def test_deployment_interval_configuration_rejects_excessive_publication_frequency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.json"
            settings = {"deployment": {"repository": "https://github.com/a/b.git"}}
            with patch.dict(os.environ, {"DATABASE_URL": "test", "SPOILLESS_PIPELINE_MODE": "shadow"}):
                for interval in (3600, 7200):
                    settings["deployment"]["minimum_interval_seconds"] = interval
                    path.write_text(json.dumps(settings))
                    self.assertEqual(Config.load(path).settings["deployment"]["minimum_interval_seconds"], interval)
                for interval in (0, 120, -1, True, "3600", 3600.5):
                    settings["deployment"]["minimum_interval_seconds"] = interval
                    path.write_text(json.dumps(settings))
                    with self.assertRaises(ValueError):
                        Config.load(path)
                settings["deployment"].pop("minimum_interval_seconds")
                for key, values in (("daily_deployment_limit", (0, 31, True, "20")),
                                    ("incremental_deployment_limit", (-1, 5, True, "4"))):
                    for value in values:
                        settings["deployment"][key] = value
                        path.write_text(json.dumps(settings))
                        with self.assertRaises(ValueError):
                            Config.load(path)
                    settings["deployment"].pop(key)

    def test_deployment_verification_waits_and_records_success(self):
        store = Mock()
        from datetime import datetime, timezone

        record = {"commit_sha": "a" * 40, "repository": "https://github.com/a/b.git", "branch": "main", "state": "pushed",
                                 "created_at": datetime.now(timezone.utc)}
        store.one.side_effect = [record, None]
        deploy = Deployment(store, Config("test"))
        job = {"payload": {"commit": "a" * 40}, "id": "job"}
        with patch("pipeline.deployment.fetch_bytes", return_value=b'{"workflow_runs":[]}'):
            with self.assertRaises(WaitingWork):
                deploy.verify(job)
        connection = Mock()
        store.transaction.return_value.__enter__ = Mock(return_value=connection)
        store.transaction.return_value.__exit__ = Mock(return_value=False)
        run = {"workflow_runs": [{"path": ".github/workflows/deploy-site.yml", "status": "completed", "conclusion": "success", "id": 123}]}
        store.one.side_effect = [record, None]
        with patch("pipeline.deployment.fetch_bytes", return_value=json.dumps(run).encode()):
            deploy.verify(job)
        store.guard.assert_called_once_with(connection, job)
        self.assertTrue(any("state='deployed'" in call.args[0] for call in connection.execute.call_args_list))
        store.one.side_effect = [record, {"commit_sha": "b" * 40}]
        run["workflow_runs"][0]["conclusion"] = "cancelled"
        with patch("pipeline.deployment.fetch_bytes", return_value=json.dumps(run).encode()):
            deploy.verify(job)
        self.assertIn("state='superseded'", connection.execute.call_args.args[0])

    def test_twitch_channel_logins_resolve_without_changing_configured_identity(self):
        channels = [{"login": "valorant", "role": "official_twitch"},
                    {"login": "gofns", "role": "watch_party", "broadcaster_user_id": "42"}]
        request = Mock(return_value=json.dumps({"data": [{"login": "valorant", "id": "123"}]}).encode())
        with patch.dict(os.environ, {"TWITCH_USER_TOKEN": "test", "TWITCH_CLIENT_ID": "client"}):
            resolved = resolve_channels(channels, request)
        self.assertEqual([item["broadcaster_user_id"] for item in resolved], ["123", "42"])
        self.assertNotIn("broadcaster_user_id", channels[0])
        self.assertEqual(resolved[0]["role"], "official_twitch")
        self.assertIn("login=valorant", request.call_args.args[0])
        self.assertNotIn("gofns", request.call_args.args[0])
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(WaitingWork):
            resolve_channels(channels, request)
        self.assertEqual(resolve_channels([channels[1]], request), [channels[1]])

    def test_map_tail_that_cannot_finish_is_not_complete(self):
        rounds = [{**series()[0], "round": number, "start": number * 100,
                   "scores": [number // 2, (number - 1) // 2]} for number in range(1, 21)]
        rounds += series(base=3000, first_map=2)
        finding = next(item for item in validate_rounds(rounds) if item["code"] == "incomplete_map")
        self.assertEqual(finding["map"], 1)
        self.assertEqual(finding["range"], [1990, 3015])

    def test_overtime_tail_requires_a_possible_two_point_win(self):
        rounds = [{**series()[0], "round": number, "start": number * 100,
                   "scores": [number // 2, (number - 1) // 2]} for number in range(1, 26)]
        self.assertTrue(any(item["code"] == "incomplete_map" for item in validate_rounds(rounds)))
        rounds.append({**rounds[-1], "round": 26, "start": 2600, "scores": [13, 12]})
        self.assertEqual(validate_rounds(rounds), [])

    def test_rss_404_falls_back_to_verified_channel_tabs_and_preserves_filters(self):
        channel = {"channel_id": "official", "includeTitle": "Champions", "excludeTitle": "HIGHLIGHTS"}
        requester = Mock(side_effect=HTTPError("feed", 404, "Not Found", {}, None))
        youtube = YouTube(requester)
        listing = {
            "channel_id": "official",
            "entries": [
                {"id": "abcdefghijk", "title": "Champions broadcast", "live_status": "is_live"},
                {"id": "abcdefghijk", "title": "Champions broadcast"},
                {"id": "lmnopqrstuv", "title": "Champions HIGHLIGHTS"},
                {"id": "mnopqrstuvw", "title": "Another event"},
                None,
            ],
        }
        with patch("yt_dlp.YoutubeDL") as factory:
            downloader = factory.return_value.__enter__.return_value
            downloader.extract_info.return_value = listing
            with self.assertLogs("pipeline.youtube", level="WARNING") as logs:
                entries = youtube.discover(channel)
                youtube.discover(channel, uploads=True)
            self.assertEqual([entry["id"] for entry in entries], ["abcdefghijk"])
            self.assertEqual(entries[0]["live_status"], "is_live")
            self.assertEqual(len(logs.output), 1)
            requester.assert_called_once()
            self.assertEqual(
                [call.args[0] for call in downloader.extract_info.call_args_list],
                ["https://www.youtube.com/channel/official/streams", "https://www.youtube.com/channel/official/videos"],
            )
            self.assertEqual(factory.call_args.args[0]["playlistend"], 30)
            self.assertTrue(factory.call_args.args[0]["skip_download"])

    def test_rss_success_keeps_existing_entry_limit_and_filters(self):
        items = "".join(
            f'<entry><yt:videoId>{number:011d}</yt:videoId><title>Champions</title><published>2026-10-04T10:00:00Z</published></entry>'
            for number in range(35)
        )
        feed = f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">{items}</feed>'.encode()
        requester = Mock(return_value=feed)
        channel = {"channel_id": "official", "includeTitle": "Champions", "excludeTitle": "HIGHLIGHTS"}
        with patch("yt_dlp.YoutubeDL") as downloader:
            youtube = YouTube(requester)
            self.assertEqual(len(youtube.discover(channel)), 30)
            youtube.discover(channel, uploads=True)
            requester.assert_called_once()
            downloader.assert_not_called()

    def test_discovery_failure_is_classified_and_rss_is_retried_after_cooldown(self):
        import yt_dlp

        requester = Mock(side_effect=HTTPError("feed", 404, "Not Found", {}, None))
        youtube = YouTube(requester)
        channel = {"channel_id": "official", "includeTitle": "Champions", "excludeTitle": "HIGHLIGHTS"}
        with patch("pipeline.youtube.time.monotonic", side_effect=[0, 1, 1801]):
            with patch("yt_dlp.YoutubeDL") as factory:
                factory.return_value.__enter__.return_value.extract_info.side_effect = yt_dlp.utils.DownloadError("Channel temporarily unavailable")
                with self.assertLogs("pipeline.youtube", level="WARNING") as logs:
                    for _ in range(3):
                        with self.assertRaisesRegex(WaitingSource, "discovery unavailable.*tab streams"):
                            youtube.discover(channel)
                self.assertEqual(requester.call_count, 2)
                self.assertEqual(len(logs.output), 2)

    def test_real_plugin_startup_keeps_pot_providers_registered_with_two_slots(self):
        script = """
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from unittest.mock import Mock
from pipeline.config import Config
from pipeline.worker import Worker
import yt_dlp
from yt_dlp.extractor.youtube.pot._registry import _pot_providers
worker = Worker(Mock(), Config('postgresql://test'), storage=Mock(), coordinator=Mock(), processing=Mock())
worker.stop.set()
worker.run()
worker.run()
def construct(_):
    with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}):
        return True
with ThreadPoolExecutor(max_workers=2) as executor:
    assert all(executor.map(construct, range(8)))
try:
    has_bgutil = importlib.util.find_spec('yt_dlp_plugins.extractor.getpot_bgutil_http') is not None
except ModuleNotFoundError:
    has_bgutil = False
if has_bgutil:
    assert 'BgUtilHTTP' in _pot_providers.value
    assert 'BgUtilScriptNode' in _pot_providers.value
"""
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "indexer")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("already registered", result.stderr)
        self.assertNotIn("Error while importing module", result.stderr)

    def test_database_connection_timeout_is_bounded_and_respects_configuration(self):
        for database, expected in [("postgresql://test@localhost/test", "5"), ("postgresql://test@localhost/test?connect_timeout=17", "17")]:
            with patch("pipeline.store.psycopg.connect") as connect:
                with Store(database).transaction():
                    pass
                self.assertEqual(connect.call_args.kwargs["connect_timeout"], expected)

    def test_plugins_initialize_once_before_processing_slots_are_created(self):
        from yt_dlp.globals import all_plugins_loaded

        store = Mock()
        worker = Worker(store, Config("postgresql://test"), storage=Mock(), coordinator=Mock(), processing=Mock())
        worker.stop.set()

        def initialize():
            all_plugins_loaded.value = True

        def processor(*args):
            self.assertTrue(all_plugins_loaded.value)
            return Mock()

        with patch.object(all_plugins_loaded, "value", False):
            with patch("yt_dlp.plugins.load_all_plugins", side_effect=initialize) as load:
                with patch("pipeline.worker.Processing", side_effect=processor):
                    worker.run()
                    worker.run()
                load.assert_called_once()

    def test_internal_status_requires_authentication_and_valid_broadcast_id(self):
        worker = Mock()
        worker.config.shadow = True
        worker.stop.is_set.return_value = False
        worker.store.status.return_value = {"broadcasts": []}
        worker.store.operator_status.return_value = {"matches": []}
        server = status_server(worker, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.dict(os.environ, {"SPOILLESS_STATUS_TOKEN": "internal-test-token"}):
                with urlopen(url + "/health", timeout=5) as response:
                    self.assertEqual(json.load(response)["state"], "ready")
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(url + "/status", timeout=5)
                self.assertEqual(rejected.exception.code, 401)
                headers = {"Authorization": "Bearer internal-test-token"}
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(Request(url + "/status?broadcast=invalid", headers=headers), timeout=5)
                self.assertEqual(invalid.exception.code, 400)
                with urlopen(Request(url + "/status", headers=headers), timeout=5) as response:
                    self.assertEqual(json.load(response), {"broadcasts": []})
                with urlopen(url + "/", timeout=5) as response:
                    self.assertIn(b"Worker status", response.read())
                    self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(url + "/operator-status", timeout=5)
                self.assertEqual(rejected.exception.code, 401)
                with urlopen(Request(url + "/operator-status", headers=headers), timeout=5) as response:
                    self.assertEqual(json.load(response), {"matches": [], "worker": {"state": "running", "mode": "shadow"}})
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(Request(url + "/shutdown", method="POST"), timeout=5)
                self.assertEqual(rejected.exception.code, 401)
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(Request(url + "/shutdown", method="POST", headers={**headers, "Origin": "http://other.test"}), timeout=5)
                self.assertEqual(rejected.exception.code, 401)
                worker.stop.set.assert_not_called()
                task_id = "00000000-0000-0000-0000-000000000001"
                retry_url = url + "/retry-job?id=" + task_id
                for request_headers in ({}, {**headers, "Origin": "http://other.test"}):
                    with self.assertRaises(HTTPError) as rejected:
                        urlopen(Request(retry_url, method="POST", headers=request_headers), timeout=5)
                    self.assertEqual(rejected.exception.code, 401)
                worker.store.retry_job.assert_not_called()
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(Request(url + "/retry-job?id=invalid", method="POST", headers=headers), timeout=5)
                self.assertEqual(invalid.exception.code, 400)
                with urlopen(Request(retry_url, method="POST", headers=headers), timeout=5) as response:
                    self.assertEqual(json.load(response), {"state": "queued"})
                self.assertTrue(worker.store.retry_job.call_args.kwargs["review_only"])
                worker.store.retry_job.side_effect = ValueError("Stale")
                with self.assertRaises(HTTPError) as stale:
                    urlopen(Request(retry_url, method="POST", headers=headers), timeout=5)
                self.assertEqual(stale.exception.code, 409)
                worker.stop.is_set.return_value = True
                with self.assertRaises(HTTPError) as stopped:
                    urlopen(Request(retry_url, method="POST", headers=headers), timeout=5)
                self.assertEqual(stopped.exception.code, 409)
                worker.stop.is_set.return_value = False
                with urlopen(Request(url + "/shutdown", method="POST", headers=headers), timeout=5) as response:
                    self.assertEqual(json.load(response), {"state": "stopping"})
                worker.stop.set.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_single_match_broadcast(self):
        segments = segment_matches(series(), [expected()])
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["findings"], [])
        self.assertEqual(segments[0]["match"]["id"], "A-B")

    def test_several_matches_in_one_broadcast(self):
        rounds = series(maps=2) + series(("C", "D"), base=8000, first_map=3)
        segments = segment_matches(rounds, [expected(best_of=3), expected(("C", "D"), order=2)])
        self.assertEqual([value["match"]["id"] for value in segments], ["A-B", "C-D"])
        self.assertTrue(all(not value["findings"] for value in segments))
        self.assertEqual(segments[1]["rounds"][0]["map"], 1)
        self.assertTrue(segments[0]["evidence"]["closed"])
        self.assertFalse(segments[1]["evidence"]["closed"])

    def test_technical_pause_inside_map(self):
        segments = segment_matches(series(pause=(6, 2000)), [expected()])
        self.assertEqual(len(segments), 1)
        self.assertFalse(segments[0]["findings"])

    def test_match_boundary_uses_consistent_later_team_readings(self):
        for missing in (1, 3):
            with self.subTest(missing=missing):
                following = series(("C", "D"), base=8000, first_map=3)
                for item in following[:missing]:
                    item["teams"] = None
                rounds = series(maps=2) + following
                segments = segment_matches(rounds, [expected(best_of=3), expected(("C", "D"), order=2)])
                self.assertEqual([value["match"]["id"] for value in segments], ["A-B", "C-D"])
                self.assertTrue(all(not value["findings"] for value in segments))
                self.assertEqual(segments[1]["rounds"][0]["round"], 1)
                self.assertEqual(segments[1]["rounds"][0]["start"], 8000)
                self.assertIsNone(segments[1]["rounds"][0]["teams"])
                self.assertTrue(segments[0]["evidence"]["closed"])

    def test_delayed_team_readings_do_not_split_same_match(self):
        following = series(base=8000, first_map=2)
        following[0]["teams"] = None
        segments = segment_matches(series() + following, [expected(best_of=3)])
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["findings"], [])

    def test_delayed_team_readings_require_consistent_repeated_identity(self):
        for labels in ([None, ["C", "D"]], [None, ["C", "D"], ["C", "D"], ["A", "B"]]):
            with self.subTest(labels=labels):
                following = series(teams=(), base=8000, first_map=2)
                for item, teams in zip(following, labels):
                    item["teams"] = teams
                segments = segment_matches(series() + following, [expected(best_of=3), expected(("C", "D"), order=2)])
                self.assertEqual(len(segments), 1)

    def test_team_change_without_map_reset_does_not_split_match(self):
        rounds = series()
        for item in rounds[5:]:
            item["teams"] = ["C", "D"]
        self.assertEqual(len(segment_matches(rounds, [expected(), expected(("C", "D"), order=2)])), 1)

    def test_long_break_is_not_sufficient_boundary(self):
        rounds = series() + series(base=10000, first_map=2)
        segments = segment_matches(rounds, [expected(best_of=3)])
        self.assertEqual(len(segments), 1)
        self.assertFalse(segments[0]["findings"])

    def test_late_start_map_counter_is_rebuilt_from_canonical_evidence(self):
        complete = [{**item, "provenance": {"method": "direct_official_archive"}} for item in series(maps=2)]
        late = [{**item, "map": 1, "provenance": {"method": "live_official_youtube"}} for item in complete[18:]]
        rebuilt = broadcast_rounds(complete + late)
        self.assertEqual(len(rebuilt), 26)
        self.assertEqual(rebuilt[-1]["map"], 2)
        self.assertEqual(rebuilt[-1]["provenance"]["method"], "live_official_youtube")

    def test_full_match_evidence_never_replaces_direct_round(self):
        live = {**series()[0], "provenance": {"method": "live_official_youtube"}}
        fallback = {**live, "start": live["start"] + 1, "provenance": {"method": "piecewise_secondary_recovery"}}
        values = broadcast_rounds([fallback, live])
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["start"], live["start"])

    def test_round_reset_without_team_identity_is_not_match_boundary(self):
        rounds = series() + series(teams=(), base=10000, first_map=2)
        self.assertEqual(len(segment_matches(rounds, [expected(best_of=3)])), 1)

    def test_replay_never_creates_round(self):
        detector = CandidateDetector()
        candidates = [detector.observe(Observation(100 + offset, 1, 100 - offset, 0.99, True)) for offset in range(3)]
        self.assertFalse(any(item["accepted"] for item in candidates))
        self.assertTrue(all("replay" in item["findings"] for item in candidates))

    def test_missing_round_produces_targeted_range(self):
        rounds = series()
        del rounds[5]
        finding = next(item for item in validate_rounds(rounds) if item["code"] == "missing_rounds")
        self.assertEqual((finding["after"], finding["before"]), (5, 7))
        self.assertLess(finding["range"][1] - finding["range"][0], 300)

    def test_early_start_regression_fixtures(self):
        fixtures = json.loads((FIXTURES / "timing.json").read_text())
        for name in ["early_frozen_clock", "early_buy_phase"]:
            with self.subTest(name=name):
                detector = CandidateDetector()
                accepted = [
                    value
                    for sample in fixtures[name]["observations"]
                    if (value := detector.observe(observation_from_dict(sample)))["accepted"]
                ]
                self.assertEqual(len(accepted), 1)
                self.assertEqual(accepted[0]["evidence"]["start"], fixtures[name]["expected_start"])
                self.assertEqual(len(accepted[0]["evidence"]["agreement"]), 3)

    def test_wrong_clock_speed_cannot_backproject_early_start(self):
        fixtures = json.loads((FIXTURES / "timing.json").read_text())
        detector = CandidateDetector()
        self.assertFalse(
            any(
                detector.observe(observation_from_dict(item))["accepted"]
                for item in fixtures["clock_speed_error"]["observations"]
            )
        )

    def test_duplicate_observations_are_idempotent(self):
        detector = CandidateDetector()
        accepted = []
        for offset in [0, 0, 1, 1, 2, 2, 3]:
            candidate = detector.observe(Observation(100 + offset, 1, 100 - offset, 0.99))
            if candidate["accepted"]:
                accepted.append(candidate)
        self.assertEqual(len(accepted), 1)

    def test_detector_resume_preserves_multiframe_agreement(self):
        original = CandidateDetector()
        original.observe(Observation(100, 1, 100, 0.99))
        original.observe(Observation(101, 1, 99, 0.99))
        resumed = CandidateDetector(json.loads(json.dumps(original.state())))
        self.assertTrue(resumed.observe(Observation(102, 1, 98, 0.99))["accepted"])
        self.assertFalse(resumed.observe(Observation(102, 1, 98, 0.99))["accepted"])

    def test_refreshed_url_keeps_source_media_time(self):
        old = parse_playlist((FIXTURES / "live.m3u8").read_text(), "https://media.example/old.m3u8")
        new = parse_playlist((FIXTURES / "refreshed.m3u8").read_text(), "https://media.example/new.m3u8")
        self.assertEqual(old[1]["wall_time"], new[0]["wall_time"])
        self.assertNotEqual(old[1]["url"], new[0]["url"])

    def test_ended_stream_waits_for_archive(self):
        value = metadata_state(
            {
                "snippet": {"liveBroadcastContent": "none"},
                "liveStreamingDetails": {
                    "actualStartTime": "2026-10-04T10:00:00Z",
                    "actualEndTime": "2026-10-04T18:00:00Z",
                },
            }
        )
        self.assertEqual(value["state"], "ended_waiting_archive")
        self.assertIsNotNone(value["actual_end"])

    def test_archive_beginning_trim(self):
        live = fingerprints(base=100)
        archive = fingerprints(base=0)
        alignment = piecewise_alignment(archive, live, maximum_distance=0)
        self.assertEqual(mapped_time(200, alignment), 100)
        with self.assertRaises(NeedsReview):
            mapped_time(50, alignment)

    def test_reconciliation_outliers_do_not_extend_verified_round_coverage(self):
        live = fingerprints(count=60)
        archive = {**live, "interval": 2, "frames": [{**frame, "time": frame["time"] + (6 if index in {0, 1, 59} else 0)} for index, frame in enumerate(live["frames"])]}
        alignment = piecewise_alignment(archive, live, maximum_distance=0)
        self.assertEqual(mapped_time(300, alignment), 300)
        for time in (0, 10, 590):
            with self.assertRaises(NeedsReview):
                mapped_time(time, alignment)

    def test_piecewise_timeline_rewrite(self):
        live = fingerprints(offsets=lambda index: 0 if index < 60 else 300)
        archive = fingerprints()
        alignment = piecewise_alignment(archive, live, maximum_distance=0)
        self.assertEqual(mapped_time(300, alignment), 300)
        self.assertEqual(mapped_time(1000, alignment), 700)
        with self.assertRaises(NeedsReview):
            mapped_time(750, alignment)

    def test_edited_full_match_pause_removed_requires_piecewise_mapping(self):
        official = fingerprints(base=1000, offsets=lambda index: 0 if index < 60 else 120)
        upload = fingerprints()
        alignment = piecewise_alignment(official, upload, maximum_distance=0)
        self.assertEqual(mapped_time(300, alignment), 1300)
        self.assertEqual(mapped_time(900, alignment), 2020)
        self.assertEqual(len(alignment["segments"]), 2)

    def test_local_alignment_preserves_pause_cut_and_unmapped_gap(self):
        canonical = fingerprints(base=1000, offsets=lambda index: 0 if index < 60 else 120)
        source = fingerprints()
        canonical["frames"] = [value for value in canonical["frames"] if not 500 <= value["time"] - 1000 <= 590]
        alignment = piecewise_alignment(canonical, source, maximum_distance=0, local=True)
        self.assertEqual(mapped_time(300, alignment), 1300)
        self.assertEqual(mapped_time(900, alignment), 2020)
        with self.assertRaises(NeedsReview):
            mapped_time(550, alignment)

    def test_local_alignment_tolerates_trimmed_edges_and_partial_broadcast(self):
        canonical, source = fingerprints(base=1000), fingerprints()
        canonical["frames"] = canonical["frames"][:60]
        source["frames"] = source["frames"][10:-10]
        alignment = piecewise_alignment(canonical, source, maximum_distance=0, local=True)
        self.assertEqual(mapped_time(300, alignment), 1300)
        for time in (0, 900, 1190):
            with self.assertRaises(NeedsReview):
                mapped_time(time, alignment)

    def test_ambiguous_local_alignment_requires_review(self):
        frames = {"frames": [{"time": index * 10, "hash": "00" * 8} for index in range(120)], "duration": 1200, "interval": 10}
        with self.assertRaises(NeedsReview):
            piecewise_alignment(frames, frames, local=True)

    def test_declared_storyboard_interval_does_not_stretch_tiles_to_duration(self):
        import cv2
        import numpy as np
        from storyboard_align import extract_storyboard

        _, image = cv2.imencode(".jpg", np.zeros((90, 800, 3), dtype=np.uint8))
        info = {"id": "fixture", "duration": 101, "formats": [{"format_id": "sb0", "width": 160, "height": 90, "rows": 1, "columns": 5, "storyboard_interval": 10, "fragments": [{"url": "sheet", "duration": 101 / 3}] * 3}]}
        with patch("storyboard_align.extract_video_info", return_value=info):
            target = extract_storyboard("fixture", "youtube", Mock(), requester=lambda url: image.tobytes(), precise_timestamps=True)
        self.assertEqual([value["time"] for value in target["frames"]], list(range(0, 101, 10)))
        self.assertEqual(target["timestampBasis"], "declared_interval")
        del info["formats"][0]["storyboard_interval"]
        with patch("storyboard_align.extract_video_info", return_value=info):
            with self.assertRaisesRegex(ValueError, "verified sampling interval"):
                extract_storyboard("fixture", "youtube", Mock(), precise_timestamps=True)

    def test_discontinuous_twitch_alignment_to_youtube(self):
        official = fingerprints()
        twitch = fingerprints(base=300, offsets=lambda index: 0 if index < 60 else 500)
        alignment = piecewise_alignment(official, twitch, maximum_distance=0)
        self.assertEqual(mapped_time(1200 + 300, alignment), 700)
        self.assertEqual(mapped_time(500, alignment), 200)

    def test_late_worker_start_is_quarantined_for_recovery(self):
        segment = segment_matches(series()[5:], [expected()])[0]
        self.assertEqual(segment["state"], "needs_review")
        self.assertTrue(any(item["code"] == "late_start" for item in segment["findings"]))

    def test_failed_match_does_not_discard_successful_match(self):
        first = series()
        del first[5]
        segments = segment_matches(
            first + series(("C", "D"), base=8000, first_map=2), [expected(), expected(("C", "D"), order=2)]
        )
        self.assertEqual([value["state"] for value in segments], ["needs_review", "validating"])

    def test_single_frame_is_never_final(self):
        self.assertFalse(CandidateDetector().observe(Observation(100, 1, 100, 0.99))["accepted"])

    def test_impossible_scores_are_retained_as_findings(self):
        candidate = CandidateDetector().observe(Observation(100, 4, 100, 0.99, scores=(5, 5)))
        self.assertIn("score_round_disagreement", candidate["findings"])
        self.assertFalse(candidate["accepted"])

    def test_bounded_retry_backoff(self):
        self.assertEqual(backoff(0), 30)
        self.assertEqual(backoff(100), 3600)

    def test_full_match_cannot_own_contract(self):
        with self.assertRaises(NeedsReview):
            canonical_contract(
                {"role": "full_match", "provider": "youtube", "broadcast_id": "x"}, expected(), {"broadcast_id": "x"}
            )

    def test_invalid_hls_sources_do_not_guess_offsets(self):
        with self.assertRaises(Unsupported):
            parse_playlist("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128\n", "https://example.com")
        with self.assertRaises(WaitingSource):
            parse_playlist("<html>expired</html>", "https://example.com")
        values = parse_playlist("#EXTM3U\n#EXTINF:6,\nx.ts\n", "https://example.com")
        self.assertIsNone(values[0]["wall_time"])

    def test_hls_master_selects_540p_variant(self):
        master = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000,RESOLUTION=1280x720\n720.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=960x540\n540.m3u8\n"
        requests = []

        def requester(url, *_):
            requests.append(url)
            return master if len(requests) == 1 else (FIXTURES / "live.m3u8").read_bytes()

        values = MediaAnalysis(requester=requester).manifest({"url": "https://media.example/master.m3u8"})
        self.assertEqual(requests[-1], "https://media.example/540.m3u8")
        self.assertEqual(len(values), 3)

    def test_dvr_start_sequence_survives_master_variant_selection(self):
        master = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=960x540\n540.m3u8?token=fixture\n"
        requests = []

        def requester(url, *_):
            requests.append(url)
            return master if len(requests) == 1 else (FIXTURES / "live.m3u8").read_bytes()

        values = MediaAnalysis(requester=requester).manifest(
            {"url": "https://media.example/master.m3u8?token=fixture&start_seq=123"}, start_sequence=0
        )
        self.assertEqual(requests, [
            "https://media.example/master.m3u8?token=fixture&start_seq=0",
            "https://media.example/540.m3u8?token=fixture&start_seq=0",
        ])
        self.assertEqual(len(values), 3)

    def test_live_presentation_clock_preserves_embedded_video_pts(self):
        import imageio_ffmpeg

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clock.ts"
            subprocess.run(
                [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=c=black:s=160x90:r=30", "-t", "1", "-c:v", "libx264", "-bf", "0",
                 "-output_ts_offset", "2150.123", "-mpegts_copyts", "1", str(path)],
                check=True, capture_output=True,
            )
            media = MediaAnalysis(requester=lambda url, headers: path.read_bytes())
            self.assertAlmostEqual(media.live_presentation_time({"headers": {}}, {"url": "fixture"}), 2150.123, places=3)

    def test_live_presentation_clock_requires_decodable_timestamped_video(self):
        media = MediaAnalysis(requester=lambda url, headers: b"invalid media")
        with self.assertRaises(WaitingSource):
            media.live_presentation_time({"headers": {}}, {"url": "fixture"})
        media = MediaAnalysis(requester=Mock(side_effect=HTTPError("fixture", 403, "Forbidden", {}, None)))
        with self.assertRaises(HTTPError):
            media.live_presentation_time({"headers": {}}, {"url": "fixture"})

    def test_live_chat_exports_existing_contract_and_strict_piecewise_mapping(self):
        source = {"external_id": "123456789"}
        messages = [
            {
                "media_time": 20,
                "origin": "live",
                "message": {"user": "Viewer", "color": "#ff0000", "message": {"text": "hello"}},
            }
        ]
        output = compact_chat(source, messages)
        self.assertEqual(output["messages"][0], {"t": 20, "u": "Viewer", "c": "#ff0000", "f": [["hello"]]})
        alignment = chat_alignment(
            {"timelineScale": 1, "segments": [{"offset": -100, "canonicalStart": 0, "canonicalEnd": 200}]}, "123456789"
        )
        self.assertEqual(alignment["segments"][0]["offset"], 100)
        self.assertTrue(alignment["strictCoverage"])

    def test_local_storage_atomic_and_no_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalStorage(directory)
            storage.put("chat/x.json", b"first")
            storage.put("chat/x.json", b"second")
            self.assertEqual(storage.get("chat/x.json"), b"second")
            with self.assertRaises(ValueError):
                storage.put("../escape.json", b"x")

    def test_riot_adapter_uses_observed_official_schema(self):
        events = json.loads((FIXTURES / "riot-events.json").read_text(encoding="utf-8"))
        data = ("<script>transport.push(" + json.dumps(events, separators=(",", ":")) + ")</script>").encode()
        provider = RiotSchedule(
            [{"league": "champions", "channel_id": "official", "region": "international", "timezone": "Asia/Shanghai"}],
            requester=lambda *_: data,
        )
        values = provider.matches()
        self.assertEqual(len(values), 2)
        self.assertEqual([value["match_order"] for value in values], [1, 2])
        self.assertEqual(values[0]["team_a"], "NRG")
        self.assertEqual(values[0]["best_of"], 3)
        self.assertNotIn("start", values[0])
        with self.assertRaises(ValueError):
            riot_events("<html>Schema changed</html>")

    def test_team_identity_requires_two_confident_names(self):
        aliases = {"PRX": ["Paper Rex", "PRX"], "NRG": ["NRG"]}
        self.assertEqual(recognized_teams([("PRX", 0.99), ("NRG", 0.99)], aliases), ["NRG", "PRX"])
        self.assertIsNone(recognized_teams([("PRX", 0.99)], aliases))

    def test_eventsub_subscription_shape(self):
        calls = []
        with patch.dict(
            os.environ, {"TWITCH_USER_TOKEN": "test", "TWITCH_CLIENT_ID": "test", "TWITCH_CHAT_USER_ID": "7"}
        ):
            subscribe("session", [{"broadcaster_user_id": "8"}], requester=calls.append)
        self.assertEqual(
            [value["type"] for value in calls], ["stream.online", "stream.offline", "channel.chat.message"]
        )
        self.assertEqual(calls[-1]["condition"], {"broadcaster_user_id": "8", "user_id": "7"})

    def test_youtube_format_unavailability_is_retryable(self):
        import yt_dlp

        with patch.object(
            yt_dlp.YoutubeDL, "extract_info", side_effect=yt_dlp.utils.DownloadError("No video formats found")
        ):
            with self.assertRaises(WaitingSource):
                YouTube().resolve("abcdefghijk")

    def test_rejected_youtube_archive_uses_public_android_without_affecting_live(self):
        import yt_dlp

        seen = []

        def extract(downloader, *args, **kwargs):
            seen.append(downloader.params)
            return {"url": "https://media.invalid/archive", "height": 360, "duration": 3600}

        with patch.dict(os.environ, {"VODLOCK_YOUTUBE_COOKIES": "fixture", "VODLOCK_YOUTUBE_POT": "1"}), patch.object(yt_dlp.YoutubeDL, "extract_info", autospec=True, side_effect=extract):
            youtube = YouTube()
            youtube.resolve("abcdefghijk", cached=True)
            youtube.reject("abcdefghijk")
            remote = youtube.resolve("abcdefghijk", cached=True)
            self.assertEqual(remote["player_client"], "android")
            self.assertEqual(remote["height"], 360)
            self.assertNotIn("cookiefile", seen[-1])
            self.assertEqual(seen[-1]["extractor_args"]["youtube"]["player_client"], ["android"])
            self.assertIn("youtubepot-bgutilhttp", seen[-1]["extractor_args"])
            youtube.resolve("abcdefghijk", live=True)
            self.assertEqual(seen[-1]["cookiefile"], "fixture")
            self.assertNotIn("youtube", seen[-1].get("extractor_args", {}))
            self.assertEqual(YouTube(archive_client="android").resolve("differentid")["player_client"], "android")

    def test_ffmpeg_access_errors_redact_signed_urls_without_hiding_status(self):
        import io

        def start(command, **kwargs):
            kwargs["stderr"].write(b"Error opening input https://media.invalid/video?sig=secret&token=secret\nServer returned 403 Forbidden\n")
            return Mock(stdout=io.BytesIO(), wait=Mock(return_value=1), poll=Mock(return_value=1))

        with patch("pipeline.media.imageio_ffmpeg.get_ffmpeg_exe", return_value="fixture"), patch("pipeline.media.subprocess.Popen", side_effect=start), self.assertRaises(WaitingSource) as failure:
            list(decode_frames("fixture", 1))
        self.assertIn("403 Forbidden", str(failure.exception))
        self.assertNotIn("secret", str(failure.exception))
        self.assertNotIn("media.invalid", str(failure.exception))

    def test_high_resolution_recovery_retries_low_quality_android_with_public_client(self):
        import yt_dlp

        seen = []
        def extract(downloader, *args, **kwargs):
            seen.append(downloader.params)
            android = downloader.params.get("extractor_args", {}).get("youtube", {}).get("player_client") == ["android"]
            return {"url": "https://media.invalid/archive", "height": 360 if android else 720, "duration": 3600}
        with patch.object(yt_dlp.YoutubeDL, "extract_info", autospec=True, side_effect=extract):
            youtube = YouTube(archive_client="android")
            self.assertEqual(youtube.resolve("abcdefghijk", height=720, cached=True)["height"], 360)
            remote = youtube.resolve("abcdefghijk", height=720, minimum_height=720, cached=True)
            self.assertEqual(remote["height"], 720)
            self.assertIsNone(remote["player_client"])
            self.assertEqual(len(seen), 3)
            self.assertNotIn("youtube", seen[-1].get("extractor_args", {}))
            self.assertEqual(youtube.resolve("abcdefghijk", height=720, minimum_height=720, cached=True)["height"], 720)
            self.assertEqual(len(seen), 3)

    def test_high_resolution_recovery_rejects_insufficient_public_video(self):
        import yt_dlp

        with patch.object(yt_dlp.YoutubeDL, "extract_info", return_value={"url": "https://media.invalid/archive", "height": 360}):
            with self.assertRaisesRegex(WaitingSource, "resolution is insufficient"):
                YouTube(archive_client="android").resolve("abcdefghijk", height=720, minimum_height=720)

    def test_archive_resolution_cache_reuses_isolated_results_until_ttl(self):
        import yt_dlp

        youtube = YouTube()
        with patch("pipeline.youtube.time.monotonic", return_value=100) as clock:
            with patch.object(yt_dlp.YoutubeDL, "extract_info", return_value={"url": "https://media.invalid/archive", "duration": 3600, "http_headers": {"User-Agent": "fixture"}}) as extract:
                first = youtube.resolve("abcdefghijk", cached=True)
                first["headers"]["User-Agent"] = "changed"
                self.assertEqual(youtube.resolve("abcdefghijk", cached=True)["headers"]["User-Agent"], "fixture")
                extract.assert_called_once()
                clock.return_value = 400
                youtube.resolve("abcdefghijk", cached=True)
                self.assertEqual(extract.call_count, 2)

    def test_encoded_video_identity_is_stable_for_archives_and_disabled_for_live_media(self):
        import yt_dlp

        info = {"url": "https://media.invalid/archive", "duration": 3600, "height": 720, "format_id": "136"}
        with patch.object(yt_dlp.YoutubeDL, "extract_info", return_value=info):
            youtube = YouTube()
            first = youtube.resolve("abcdefghijk")
            info["url"] = "https://media.invalid/refreshed"
            self.assertEqual(youtube.resolve("abcdefghijk")["cache_identity"], first["cache_identity"])
            self.assertEqual(first["cache_identity"], ("youtube", "abcdefghijk", "136", 720, 3600))
            info["format_id"] = "135"
            self.assertNotEqual(youtube.resolve("abcdefghijk")["cache_identity"], first["cache_identity"])
            self.assertIsNone(youtube.resolve("abcdefghijk", live=True)["cache_identity"])
            info["is_live"] = True
            self.assertIsNone(youtube.resolve("abcdefghijk")["cache_identity"])

    def test_archive_resolution_cache_respects_signed_expiry_and_invalid_expiry(self):
        import yt_dlp

        for expiry, expected in [("1250", 2), ("1150", 3), ("nan", 3), ("invalid", 3)]:
            with self.subTest(expiry=expiry):
                youtube = YouTube()
                with patch("pipeline.youtube.time.monotonic", return_value=100) as clock, patch("pipeline.youtube.time.time", return_value=1000):
                    with patch.object(yt_dlp.YoutubeDL, "extract_info", return_value={"url": "https://media.invalid/archive?expire=" + expiry}) as extract:
                        youtube.resolve("abcdefghijk", cached=True)
                        youtube.resolve("abcdefghijk", cached=True)
                        clock.return_value = 171
                        youtube.resolve("abcdefghijk", cached=True)
                        self.assertEqual(extract.call_count, expected)

    def test_archive_resolution_cache_separates_formats_and_bypasses_live_and_fresh_probes(self):
        import yt_dlp

        youtube = YouTube()
        with patch.object(yt_dlp.YoutubeDL, "extract_info", return_value={"url": "https://media.invalid/archive"}) as extract:
            youtube.resolve("abcdefghijk", height=540, cached=True)
            youtube.resolve("abcdefghijk", height=720, cached=True)
            youtube.resolve("abcdefghijk", height=540, cached=True)
            self.assertEqual(extract.call_count, 2)
            youtube.resolve("abcdefghijk")
            youtube.resolve("abcdefghijk", height=720, cached=True)
            youtube.resolve("abcdefghijk", live=True, cached=True)
            youtube.resolve("abcdefghijk", live=True, cached=True)
            self.assertEqual(extract.call_count, 6)

    def test_archive_resolution_failure_is_not_cached_and_invalidation_refreshes_urls(self):
        import yt_dlp

        youtube = YouTube()
        with patch.object(yt_dlp.YoutubeDL, "extract_info", side_effect=[yt_dlp.utils.DownloadError("403 Forbidden"), {"url": "https://media.invalid/old"}, {"url": "https://media.invalid/new"}]) as extract:
            with self.assertRaises(WaitingSource):
                youtube.resolve("abcdefghijk", cached=True)
            self.assertEqual(youtube.resolve("abcdefghijk", cached=True)["url"], "https://media.invalid/old")
            youtube.invalidate("abcdefghijk")
            self.assertEqual(youtube.resolve("abcdefghijk", cached=True)["url"], "https://media.invalid/new")
            self.assertEqual(extract.call_count, 3)

    def test_youtube_archive_uses_default_clients_with_existing_authentication(self):
        import yt_dlp

        def extract(downloader, *args, **kwargs):
            self.assertNotIn("youtube", downloader.params.get("extractor_args", {}))
            self.assertEqual(downloader.params["cookiefile"], cookiefile)
            self.assertEqual(
                downloader.params["extractor_args"]["youtubepot-bgutilhttp"]["base_url"], ["http://127.0.0.1:4416"]
            )
            return downloader.process_ie_result(
                {
                    "id": "abcdefghijk",
                    "title": "Broadcast",
                    "duration": 3600,
                    "formats": [
                        {
                            "url": f"https://media.invalid/{height}.mp4",
                            "height": height,
                            "ext": "mp4",
                            "vcodec": "avc1",
                            "acodec": "none",
                        }
                        for height in [480, 720, 1080]
                    ],
                },
                download=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            cookiefile = str(Path(directory) / "operator-cookies.txt")
            with patch.dict(os.environ, {"VODLOCK_YOUTUBE_POT": "1", "VODLOCK_YOUTUBE_COOKIES": cookiefile}):
                with patch.object(yt_dlp.YoutubeDL, "extract_info", autospec=True, side_effect=extract):
                    remote = YouTube().resolve("abcdefghijk", height=720)
        self.assertEqual(remote["url"], "https://media.invalid/720.mp4")

    def test_youtube_live_selects_video_only_hls_with_bounded_height(self):
        import yt_dlp

        for heights, expected in [([480, 720, 1080], 480), ([720, 1080], 720)]:
            with self.subTest(heights=heights):

                def extract(downloader, *args, **kwargs):
                    self.assertNotIn("youtube", downloader.params.get("extractor_args", {}))
                    return downloader.process_ie_result(
                        {
                            "id": "abcdefghijk",
                            "title": "Live broadcast",
                            "formats": [
                                {
                                    "url": f"https://media.invalid/{height}.m3u8",
                                    "height": height,
                                    "ext": "mp4",
                                    "protocol": "m3u8_native",
                                    "vcodec": "avc1",
                                    "acodec": "none",
                                }
                                for height in heights
                            ]
                            + [
                                {
                                    "url": "https://media.invalid/direct.mp4",
                                    "height": 540,
                                    "ext": "mp4",
                                    "protocol": "https",
                                    "vcodec": "avc1",
                                    "acodec": "none",
                                }
                            ],
                        },
                        download=False,
                    )

                with patch.dict(os.environ, {"VODLOCK_YOUTUBE_POT": "0", "VODLOCK_YOUTUBE_COOKIES": ""}):
                    with patch.object(yt_dlp.YoutubeDL, "extract_info", autospec=True, side_effect=extract):
                        remote = YouTube().resolve("abcdefghijk", live=True)
                self.assertEqual(remote["url"], f"https://media.invalid/{expected}.m3u8")

    def test_shadow_default_and_publish_requires_review(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test", "SPOILLESS_PIPELINE_MODE": "shadow"}):
            self.assertTrue(Config.load().shadow)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test", "SPOILLESS_PIPELINE_MODE": "publish"}):
            with self.assertRaises(ValueError):
                Config.load()

    def test_archive_start_configuration_is_per_video_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.json"
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test", "SPOILLESS_PIPELINE_MODE": "shadow"}):
                path.write_text(json.dumps({"archive_start_seconds": {"abcdefghijk": 1800}}))
                config = Config.load(path)
                self.assertEqual(config.archive_start_for({"youtube_id": "abcdefghijk"}), 1800)
                self.assertEqual(config.archive_start_for({"youtube_id": "other"}), 0)
                for value in [-1, True, "1800", float("inf"), float("nan")]:
                    path.write_text(json.dumps({"archive_start_seconds": {"abcdefghijk": value}}))
                    with self.assertRaises(ValueError):
                        Config.load(path)

    def test_processing_slot_configuration_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.json"
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test", "SPOILLESS_PIPELINE_MODE": "shadow"}):
                self.assertEqual(Config.load().processing_slots, 2)
                path.write_text(json.dumps({"processing_slots": 1}))
                self.assertEqual(Config.load(path).processing_slots, 1)
                for value in [0, -1, 3, True, "2", 2.5]:
                    path.write_text(json.dumps({"processing_slots": value}))
                    with self.assertRaises(ValueError):
                        Config.load(path)

    def test_compare_reports_coverage_and_accuracy(self):
        legacy = {"a": {"sourceId": "official", "rounds": series()}, "missing": {"rounds": series()}}
        shadow = {"a": {"sourceId": "official", "rounds": [{**item, "start": item["start"] + 15} for item in series()]}}
        report = compare_indexes(legacy, shadow)
        self.assertEqual(report["missing_matches"], ["missing"])
        self.assertEqual(report["median_difference"], 15)
        self.assertEqual(report["worst_difference"], 15)

    def test_release_rejects_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release, site = root / "release", root / "site"
            release.mkdir()
            site.mkdir()
            entry = {
                "expectedMatchId": "match",
                "sourceId": "official",
                "title": "A vs B",
                "pipelineGeneration": 1,
                "pipelineVersion": 1,
            }
            (release / "catalog.json").write_text(json.dumps({"videos": [entry]}))
            (site / "catalog.json").write_text(json.dumps({"videos": [{**entry, "pipelineVersion": 2}]}))
            with self.assertRaises(ValueError):
                release_to_site(release, site)

    def test_partial_release_preserves_other_matches_in_same_broadcast(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release, site = root / "release", root / "site"
            release.mkdir()
            site.mkdir()
            (release / "indexes").mkdir()
            (release / "indexes" / "match.json").write_text(json.dumps({"schemaVersion": 2, "provider": "youtube", "sourceId": "official", "pipelineState": "provisional", "pipelineVersion": 2, "rounds": series()}))
            first = {
                "provider": "youtube",
                "sourceId": "official",
                "title": "A vs B",
                "expectedMatchId": "a",
                "index": "/indexes/match.json",
                "pipelineGeneration": 1,
                "pipelineVersion": 2,
                "pipelineState": "provisional",
                "playedAt": "2026-10-04T10:00:00Z",
            }
            second = {**first, "title": "C vs D", "expectedMatchId": "b", "pipelineVersion": 1}
            (release / "catalog.json").write_text(json.dumps({"videos": [first]}))
            legacy = {"provider": "youtube", "sourceId": "official", "title": "B vs A", "index": "/indexes/legacy.json"}
            (site / "catalog.json").write_text(json.dumps({"videos": [{**first, "pipelineVersion": 1}, second, legacy]}))
            release_to_site(release, site)
            catalog = json.loads((site / "catalog.json").read_text())
            self.assertEqual(len(catalog["videos"]), 2)
            self.assertTrue(any(item["expectedMatchId"] == "b" for item in catalog["videos"]))

    def test_release_checks_all_artifacts_before_replacing_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release, site = root / "release", root / "site"
            release.mkdir()
            site.mkdir()
            (release / "indexes").mkdir()
            index = {"schemaVersion": 2, "provider": "youtube", "sourceId": "abcdefghijk", "pipelineState": "final", "pipelineVersion": 1, "rounds": series()}
            (release / "indexes" / "first.json").write_text(json.dumps(index))
            entry = {"provider": "youtube", "sourceId": "abcdefghijk", "pipelineState": "final", "pipelineGeneration": 1, "pipelineVersion": 1, "title": "A vs B", "expectedMatchId": "first", "index": "/indexes/first.json"}
            original = json.dumps({"videos": []})
            (site / "catalog.json").write_text(original)
            for path in ["/indexes/missing.json", "/indexes/../../outside.json"]:
                (release / "catalog.json").write_text(json.dumps({"videos": [entry, {**entry, "expectedMatchId": "second", "index": path}]}))
                with self.assertRaises((ValueError, FileNotFoundError)):
                    release_to_site(release, site)
                self.assertEqual((site / "catalog.json").read_text(), original)
                self.assertFalse((site / "indexes").exists())
            (release / "catalog.json").write_text(json.dumps({"videos": [{**entry, "sourceId": "wrongsource"}]}))
            with self.assertRaisesRegex(ValueError, "canonical catalog"):
                release_to_site(release, site)

    def test_release_withholds_invalidated_index_without_removing_newer_or_legacy_data(self):
        with tempfile.TemporaryDirectory() as directory:
            release, site = Path(directory) / "release", Path(directory) / "site"
            release.mkdir()
            site.mkdir()
            entries = [
                {"provider": "youtube", "sourceId": "canonical01", "title": "A vs B", "expectedMatchId": "match",
                 "canonicalPipeline": True, "pipelineGeneration": 1, "pipelineVersion": 2},
                {"provider": "youtube", "sourceId": "historical1", "title": "C vs D", "expectedMatchId": "legacy"},
            ]
            for version in [1, 3]:
                (site / "catalog.json").write_text(json.dumps({"version": 2, "videos": entries}))
                (release / "catalog.json").write_text(json.dumps({"version": 2, "videos": [], "withheld": [
                    {"expectedMatchId": "match", "pipelineGeneration": 1, "pipelineVersion": version},
                    {"expectedMatchId": "legacy", "pipelineGeneration": 1, "pipelineVersion": 999},
                ]}))
                release_to_site(release, site)
                installed = json.loads((site / "catalog.json").read_text())["videos"]
                self.assertIn(entries[1], installed)
                self.assertEqual(entries[0] in installed, version == 1)
            entry = {**entries[0], "pipelineState": "provisional", "index": "/indexes/corrected.json"}
            (release / "catalog.json").write_text(json.dumps({"version": 2, "videos": [entry]}))
            with self.assertRaisesRegex(ValueError, "older"):
                release_to_site(release, site)
            entry["pipelineVersion"] = 4
            (release / "indexes").mkdir()
            (release / "indexes" / "corrected.json").write_text(json.dumps({
                "schemaVersion": 2, "provider": "youtube", "sourceId": entry["sourceId"],
                "pipelineState": "provisional", "pipelineVersion": 4,
                "rounds": [{"map": 1, "round": 1, "start": 100}],
            }))
            (release / "catalog.json").write_text(json.dumps({"version": 2, "videos": [entry]}))
            release_to_site(release, site)
            installed = json.loads((site / "catalog.json").read_text())
            self.assertEqual(installed["withheld"], [])
            self.assertEqual(len(installed["videos"]), 2)

    def test_archive_reconciliation_does_not_invoke_ocr(self):
        alignment = piecewise_alignment(fingerprints(), fingerprints(base=100), maximum_distance=0)
        rounds = reconcile_rounds([{"map": 1, "round": 1, "start": 200}], alignment)
        self.assertEqual(rounds[0]["start"], 100)
        self.assertEqual(rounds[0]["live_start"], 200)

    def test_intro_overlay_requires_countdown_and_matchup(self):
        import numpy as np

        reader = Mock()
        media = MediaAnalysis(reader=reader)
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        for lines, expected in [
            ([("00:15:43", .99), ("TLVSPRX", .99)], True),
            ([("00:11:26", .99), ("GE VS EDG", .99)], True),
            ([("1:39", .99), ("TL VS PRX", .99)], False),
            ([("00:15:43", .99)], False),
            ([("00:15:43", .5), ("TLVSPRX", .99)], False),
        ]:
            reader.read_lines.return_value = lines
            self.assertEqual(media.intro_context(frame)["intro"], expected)
        reader.read.return_value = Observation(100, 1, 99, .99)
        reader.read_lines.return_value = [("00:15:43", .99), ("TLVSPRX", .99)]
        sample, extra = media.observation(frame, 100, compact=False)
        self.assertTrue(sample.replay)
        candidate = CandidateDetector().observe(sample, extra=extra)
        self.assertFalse(candidate["accepted"])
        self.assertIn("intro_hud", candidate["findings"])

    def test_recorded_intro_noise_does_not_shift_real_map_numbering(self):
        fixture = json.loads((FIXTURES / "intro_observations.json").read_text())
        for video, start in [("w5O5vwuHwXY", 2295), ("eKP_QB_H7F8", 2157), ("ynKvUYpu1Qw", 5438)]:
            with self.subTest(video=video):
                for row in fixture[video]:
                    row["evidence"].setdefault("intro_context", None)
                detected, states = redetect_candidates(fixture[video])
                self.assertEqual(detected[0]["round_number"], 1)
                self.assertEqual(detected[0]["evidence"]["start"], start)
                self.assertEqual(detected[0]["evidence"]["broadcast_map"], 1)
                self.assertTrue(all(value["evidence"]["start"] >= start for value in detected))
                if video == "ynKvUYpu1Qw":
                    self.assertEqual([value["round_number"] for value in detected], [1, 2, 3, 4, 5])
                    self.assertTrue(all(value["confidence"] >= .85 for value in detected))
                    detector = CandidateDetector(json.loads(json.dumps(states["archive"].state())))
                    for second in range(3):
                        value = detector.observe(Observation(6000 + second, 6, 100 - second, .99))
                    self.assertTrue(value["accepted"])
                    self.assertEqual(value["evidence"]["broadcast_map"], 1)
                else:
                    self.assertEqual({value["evidence"]["broadcast_map"] for value in detected}, {1, 2})
                    detector = states["archive"]
                    for number in (1, 2):
                        for second in range(3):
                            value = detector.observe(Observation(12000 + number * 100 + second, number, 100 - second, .99))
                    self.assertTrue(value["accepted"])
                    self.assertEqual(value["evidence"]["broadcast_map"], 3)

    def test_recorded_scoreless_map_reset_requires_next_coherent_round(self):
        fixture = json.loads((FIXTURES / "map_reset.json").read_text())
        detector = CandidateDetector({"previous": fixture["previous"]})
        accepted = []
        for value in fixture["observations"]:
            candidate = detector.observe(observation_from_dict(value), extra={"teams": value["teams"]})
            accepted.extend(detector.confirmed_candidates)
            if candidate["accepted"]:
                accepted.append(candidate)
            if value["round"] == 1:
                self.assertFalse(candidate["accepted"])
        self.assertEqual([value["round_number"] for value in accepted], [1, 2])
        self.assertEqual(accepted[0]["evidence"]["start"], fixture["expected_start"])
        self.assertEqual(accepted[0]["evidence"]["broadcast_map"], fixture["expected_map"])
        self.assertTrue(accepted[0]["evidence"]["map_reset"])

    def test_pending_scoreless_reset_survives_detector_restart(self):
        detector = CandidateDetector({"previous": {"round": 21, "start": 100}})
        for second in range(3):
            candidate = detector.observe(Observation(1000 + second, 1, 100 - second, .99))
            self.assertFalse(candidate["accepted"])
        restarted = CandidateDetector(json.loads(json.dumps(detector.state())))
        for second in range(3):
            candidate = restarted.observe(Observation(1100 + second, 2, 100 - second, .99))
        self.assertTrue(candidate["accepted"])
        self.assertEqual(restarted.confirmed_candidates[0]["evidence"]["start"], 1000)
        self.assertEqual(restarted.map_number, 2)

    def test_overlapping_recovery_samples_preserve_clock_agreement_after_restart(self):
        detector = CandidateDetector()
        for timestamp, timer in [(11824.003, 95), (11824.813, 94), (11825.003, 94)]:
            candidate = detector.observe(Observation(timestamp, 7, timer, .95))
            self.assertFalse(candidate["accepted"])
        self.assertEqual(candidate["findings"], ["repeated_clock"])
        resumed = CandidateDetector(json.loads(json.dumps(detector.state())))
        candidate = resumed.observe(Observation(11825.813, 7, 93, .96))
        self.assertTrue(candidate["accepted"])
        self.assertAlmostEqual(candidate["evidence"]["start"], 11818.813)
        self.assertEqual(len(candidate["evidence"]["agreement"]), 3)

    def test_repeated_clock_cannot_confirm_a_frozen_hud(self):
        detector = CandidateDetector()
        for index in range(12):
            candidate = detector.observe(Observation(100 + index * .2, 1, 100, .99))
            self.assertFalse(candidate["accepted"])

    def test_redetection_of_overlapping_recovery_is_idempotent(self):
        candidates = []
        for timestamp, timer in [(11824.003, 95), (11824.813, 94), (11825.003, 94),
                                 (11825.813, 93), (11826.003, 93), (11826.813, 92)]:
            candidates.append({
                "id": str(len(candidates)), "timeline": "archive", "media_time": timestamp,
                "confidence": .95, "detector_version": "canonical-clock-v3", "accepted": False,
                "evidence": {"time": timestamp, "round": 7, "timer": timer, "confidence": .95},
            })
        detected, _ = redetect_candidates(candidates + candidates)
        self.assertEqual(len(detected), 1)
        self.assertEqual(detected, redetect_candidates(candidates)[0])

    def test_archive_recovery_fragments_use_verified_live_map_context(self):
        candidates = []
        for base, number in [(100, 20), (11820, 7)]:
            for second in range(3):
                candidates.append({
                    "id": str(len(candidates)), "timeline": "archive", "media_time": base + second,
                    "confidence": .95, "detector_version": "canonical-clock-v3", "accepted": False,
                    "evidence": {"time": base + second, "round": number, "timer": 100 - second, "confidence": .95},
                })
        anchor = {"timeline": "archive", "media_time": 11702, "round_number": 6, "scores": None,
                  "evidence": {"start": 11700, "broadcast_map": 3}}
        detected, _ = redetect_candidates(candidates, [anchor])
        recovered = next(value for value in detected if value["media_time"] > 11800)
        self.assertEqual(recovered["evidence"]["broadcast_map"], 3)
        self.assertEqual(recovered["evidence"]["start"], 11820)
        self.assertFalse(any(value["media_time"] > 11800 for value in redetect_candidates(candidates)[0]))
        future = {**anchor, "media_time": 12000}
        self.assertFalse(any(value["media_time"] > 11800 for value in redetect_candidates(candidates, [future])[0]))

    def test_stored_ocr_retains_later_maps_and_missing_round_findings(self):
        for missing in (False, "gap", "tail"):
            with self.subTest(missing=missing):
                values = series(maps=2)
                if missing:
                    values = [value for value in values if not (value["map"] == 1 and value["round"] in ((1, 7) if missing == "gap" else (1, 12, 13)))]
                candidates = []
                for value in values:
                    for second in range(3):
                        sample = Observation(value["start"] + second, value["round"], 100 - second, .99)
                        candidates.append({
                            "id": str(len(candidates)), "timeline": "archive", "media_time": sample.time,
                            "confidence": .99, "detector_version": "canonical-clock-v1", "accepted": False,
                            "evidence": {"time": sample.time, "round": sample.round, "timer": sample.timer,
                                         "confidence": .99, "teams": ["A", "B"]},
                        })
                detected, _ = redetect_candidates(candidates + candidates)
                rounds = broadcast_rounds([
                    {"map": item["evidence"]["broadcast_map"], "round": item["round_number"],
                     "start": item["evidence"]["start"], "confidence": item["confidence"],
                     "scores": item["scores"], "teams": item["evidence"]["teams"],
                     "map_reset": item["evidence"].get("map_reset", False)} for item in detected
                ])
                segments = segment_matches(rounds, [{"id": "match", "team_a": "A", "team_b": "B", "match_order": 1, "best_of": 3}])
                self.assertEqual(len(segments), 1)
                self.assertEqual({item["map"] for item in segments[0]["rounds"]}, {1, 2})
                self.assertEqual(len([item for item in rounds if item["map"] == 2]), 13)
                codes = {item["code"] for item in segments[0]["findings"]}
                if missing:
                    self.assertIn("late_start", codes)
                    self.assertIn("missing_rounds" if missing == "gap" else "invalid_map_sequence", codes)

    def test_ffmpeg_sampling_retains_exact_offset(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frames.avi"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), 10, (64, 64))
            self.assertTrue(writer.isOpened())
            for index in range(60):
                writer.write(np.full((64, 64, 3), index, dtype=np.uint8))
            writer.release()
            values = list(decode_frames(path, 2, start=2, interval=2))
            self.assertEqual(values[0][0], 0)
            self.assertLessEqual(abs(float(values[0][1].mean()) - 20), 1)

    def test_reused_archive_decoder_preserves_real_ffmpeg_timestamps(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frames.avi"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), 10, (64, 64))
            self.assertTrue(writer.isOpened())
            for index in range(120):
                writer.write(np.full((64, 64, 3), index, dtype=np.uint8))
            writer.release()
            media = MediaAnalysis()
            self.addCleanup(media.close)
            remote = {"url": str(path), "duration": 12}
            earlier = [(position, float(frame.mean())) for position, frame in media.archive_frames(remote, 0, 4)]
            later = [(position, float(frame.mean())) for position, frame in media.archive_frames(remote, 4, 8)]
            self.assertEqual([position for position, _ in earlier + later], list(range(8)))
            for position, mean in earlier + later:
                self.assertLessEqual(abs(mean - position * 10), 1)
            fresh = [(4 + offset, float(frame.mean())) for offset, frame in decode_frames(path, 4, start=4, compact=False)]
            self.assertEqual(later, fresh)
            media.close()

    def test_ocr_and_archive_fingerprints_are_compatible_at_identical_media_time(self):
        import cv2
        import numpy as np
        from storyboard_align import frame_distance

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hud.avi"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), 5, (1280, 720))
            self.assertTrue(writer.isOpened())
            frame = np.random.default_rng(19).integers(0, 256, (180, 320, 3), dtype=np.uint8)
            frame = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_NEAREST)
            cv2.putText(frame, "ROUND 1 1:40", (400, 70), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 3)
            for _ in range(10):
                writer.write(frame)
            writer.release()
            analysis = MediaAnalysis()
            full = next(decode_frames(path, 2, compact=False))[1]
            recovered = {"time": 0, **analysis.fingerprint(full, compact=False)}
            archive = list(analysis.archive_fingerprints({"url": str(path)}, 0, 2, interval=2))
            self.assertEqual([value["time"] for value in archive], [0])
            self.assertLessEqual(frame_distance(recovered, archive[0]), 6)


if __name__ == "__main__":
    unittest.main()
