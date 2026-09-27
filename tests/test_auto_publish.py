import sys
import io
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))
import auto_publish


class AutoPublishTests(unittest.TestCase):
    global_channel = {"name": "Global", "includeTitle": r"\bFULL MATCH\b",
                      "excludeTitle": r"\b(HIGHLIGHTS|SHOWMATCH)\b", "minimumDuration": 3600}
    americas_channel = {"name": "Americas", "includeTitle": r"^[A-Z0-9][A-Z0-9 ._-]{1,20}\s+vs\.?\s+[A-Z0-9][A-Z0-9 ._-]{1,20}\s+[-|]",
                        "excludeTitle": r"\b(HIGHLIGHTS|MATCH POINT|SHOWMATCH|DRAW SHOW|DAY [0-9]+ FILM)\b",
                        "minimumDuration": 3600}

    def test_global_channel_accepts_only_finished_full_matches(self):
        valid = {"id": "ZphbktbT26k", "title": "LOUD vs. EDG — FULL MATCH — Champions Shanghai", "duration": 4659}
        self.assertTrue(auto_publish.is_candidate(self.global_channel, valid))
        self.assertFalse(auto_publish.is_candidate(self.global_channel, {**valid, "title": "LOUD vs EDG | MATCH HIGHLIGHTS"}))
        self.assertFalse(auto_publish.is_candidate(self.global_channel, {**valid, "duration": 1200}))
        self.assertFalse(auto_publish.is_candidate(self.global_channel, {**valid, "live_status": "is_live"}))

    def test_americas_channel_rejects_non_match_programming(self):
        valid = {"id": "TntlDvMFTX0", "title": "NRG vs 100T - VCT Americas Stage 2", "duration": 4835}
        self.assertTrue(auto_publish.is_candidate(self.americas_channel, valid))
        self.assertFalse(auto_publish.is_candidate(self.americas_channel, {**valid, "title": "Team A vs Team B - Override Showmatch"}))
        self.assertFalse(auto_publish.is_candidate(self.americas_channel, {**valid, "title": "The VCT Americas Draw Show"}))

    def test_catalog_metadata_removes_feed_markers_and_unicode_dashes(self):
        entry = {"title": "LOUD vs. EDG — FULL MATCH — VALORANT Champions Shanghai — Group Stage"}
        title, event = auto_publish.catalog_metadata(self.global_channel, entry)
        self.assertEqual(title, "LOUD vs EDG")
        self.assertEqual(event, "VALORANT Champions Shanghai | Group Stage")
        self.assertNotIn("—", title + event)

    def test_twitch_duration_and_finished_archive_filter(self):
        channel = {"includeTitle": ".+", "excludeTitle": r"\bRERUN\b", "minimumDuration": 3600}
        valid = {"id": "1234567890", "stream_id": "777", "type": "archive",
                 "title": "VCT co-stream", "duration": "6h12m4s"}
        self.assertEqual(auto_publish.twitch_duration("6h12m4s"), 22324)
        self.assertTrue(auto_publish.is_twitch_candidate(channel, valid, set()))
        self.assertFalse(auto_publish.is_twitch_candidate(channel, valid, {"777"}))
        self.assertFalse(auto_publish.is_twitch_candidate(channel, {**valid, "type": "upload"}, set()))
        self.assertFalse(auto_publish.is_twitch_candidate(channel, {**valid, "duration": "59m59s"}, set()))

    def test_twitch_discovery_rejects_current_live_stream(self):
        channels = [{"name": "FNS", "login": "gofns", "includeTitle": ".+",
                     "excludeTitle": "never", "minimumDuration": 3600}]

        def requester(url, headers=None, data=None):
            if "oauth2/token" in url:
                return {"access_token": "token"}
            if "/users?" in url:
                return {"data": [{"id": "1", "login": "gofns"}]}
            if "/streams?" in url:
                return {"data": [{"id": "live-stream"}]}
            return {"data": [
                {"id": "1234567890", "stream_id": "live-stream", "type": "archive", "title": "Live", "duration": "2h"},
                {"id": "1234567891", "stream_id": "finished-stream", "type": "archive", "title": "Finished", "duration": "2h"}
            ]}

        result = auto_publish.discover_twitch(channels, 30, "client", "secret", requester)
        self.assertEqual([item["id"] for item in result["gofns"]], ["1234567891"])

    def test_held_vods_retry_after_detector_fix_and_cooldown(self):
        now = datetime.now(timezone.utc)
        key = "twitch:1234567890"
        old = {key: {"status": "held", "checkedAt": now.isoformat()}}
        self.assertTrue(auto_publish.should_attempt(key, set(), old, 6, now))
        current = {key: {**old[key], "detectorVersion": auto_publish.DETECTOR_VERSION}}
        self.assertFalse(auto_publish.should_attempt(key, set(), current, 6, now))
        self.assertTrue(auto_publish.should_attempt(key, set(), current, 6, now + timedelta(hours=6)))
        self.assertFalse(auto_publish.should_attempt(key, {key}, old, 6, now))

    def test_automatic_publication_requires_complete_high_confidence_sequence(self):
        rounds = [{"map": 1, "round": number, "start": number * 100, "confidence": 0.9} for number in range(1, 14)]
        job = {"status": "ready", "warnings": [], "rounds": rounds}
        self.assertEqual(auto_publish.publishable(job, 0.75, 13), (True, ""))
        self.assertFalse(auto_publish.publishable({**job, "warnings": ["gap"]}, 0.75, 13)[0])
        low = [dict(item) for item in rounds]
        low[5]["confidence"] = 0.7
        self.assertFalse(auto_publish.publishable({**job, "rounds": low}, 0.75, 13)[0])
        self.assertEqual(auto_publish.publishable({**job, "rounds": low}, 0.65, 13), (True, ""))
        gap = [dict(item) for item in rounds]
        gap[5]["round"] = 7
        self.assertFalse(auto_publish.publishable({**job, "rounds": gap}, 0.75, 13)[0])
        excluded = {"map": 2, "round": 1, "start": 2000, "confidence": 0.1, "excluded": True}
        self.assertTrue(auto_publish.publishable({**job, "rounds": rounds + [excluded]}, 0.75, 13)[0])

    def test_youtube_discovery_failure_does_not_block_twitch_or_held_retries(self):
        channels = [{"provider": "youtube", "name": "YouTube"},
                    {"provider": "twitch", "name": "VALORANT", "login": "valorant"},
                    {"provider": "twitch", "name": "FNS", "login": "gofns"}]
        config = {"channels": channels, "lookback": 30, "maxPerRun": 4, "retryHours": 6}
        state = {"videos": {"twitch:1234567890": {"status": "held", "checkedAt": datetime.now(timezone.utc).isoformat()}}}
        catalog = {"videos": []}
        entries = {"valorant": [{"id": "1234567890", "title": "Official"}],
                   "gofns": [{"id": "1234567891", "title": "Watchparty"}]}
        with patch.object(auto_publish, "read_json", side_effect=[config, state, catalog]), \
                patch.object(auto_publish, "discover_youtube", side_effect=RuntimeError("blocked")), \
                patch.object(auto_publish, "discover_twitch", return_value=entries), \
                patch.object(auto_publish, "process", return_value=(True, "Published")) as process, \
                patch.object(auto_publish, "write_json"), \
                patch.dict(auto_publish.os.environ, {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret"}), \
                patch.object(sys, "argv", ["auto_publish.py"]), \
                patch("sys.stderr", new_callable=io.StringIO) as errors, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(auto_publish.main(), 0)
        self.assertEqual([call.args[1]["id"] for call in process.call_args_list], ["1234567890", "1234567891"])
        self.assertEqual(state["videos"]["twitch:1234567890"]["status"], "published")
        self.assertIn("YouTube discovery failed", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
