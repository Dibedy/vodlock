import sys
import io
import tempfile
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

    def test_youtube_feed_discovers_only_full_matches_without_scraping_channel_page(self):
        channel = {**self.global_channel, "channelId": "channel"}
        feed = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
        <entry><yt:videoId>fFfCJDNHEvc</yt:videoId><title>JDG vs. FUT - FULL MATCH - Champions Shanghai</title><published>2026-09-27T12:00:00Z</published></entry>
        <entry><yt:videoId>abcdefghijk</yt:videoId><title>JDG vs. FUT - HIGHLIGHTS</title><published>2026-09-27T13:00:00Z</published></entry></feed>'''
        result = auto_publish.discover_youtube(channel, 30, requester=lambda _: feed)
        self.assertEqual(result, [{"id": "fFfCJDNHEvc", "title": "JDG vs. FUT - FULL MATCH - Champions Shanghai",
                                   "published": "2026-09-27T12:00:00Z"}])

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

    def test_catalog_time_uses_the_first_round_in_the_original_broadcast(self):
        rounds = [{"map": 1, "round": 1, "start": 100}]
        self.assertEqual(auto_publish.catalog_played_at({"created_at": "2026-09-29T10:00:00Z"}, rounds),
                         "2026-09-29T10:01:40Z")
        source = {"publishedAt": "2026-09-29T10:00:00Z"}
        alignment = {"timelineScale": 1.002, "segments": [
            {"offset": 600, "targetStart": 0, "targetEnd": 1000},
            {"offset": 900, "targetStart": 1000, "targetEnd": 2000}]}
        self.assertEqual(auto_publish.catalog_played_at({}, rounds, source, alignment),
                         "2026-09-29T10:11:40Z")

    def test_held_diagnostics_are_copied_out_of_the_temporary_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "job"
            source = work / "diagnostics"
            source.mkdir(parents=True)
            (source / "hud-00005.jpg").write_bytes(b"image")
            with patch.object(auto_publish, "DIAGNOSTICS", root / "saved"):
                auto_publish.preserve_diagnostics("twitch", "1234567890", work)
            self.assertEqual((root / "saved" / "twitch-1234567890" / "hud-00005.jpg").read_bytes(), b"image")

    def test_twitch_duration_and_finished_archive_filter(self):
        channel = {"includeTitle": ".+", "excludeTitle": r"\bRERUN\b", "minimumDuration": 3600}
        valid = {"id": "1234567890", "stream_id": "777", "type": "archive",
                 "title": "VCT co-stream", "duration": "6h12m4s"}
        self.assertEqual(auto_publish.twitch_duration("6h12m4s"), 22324)
        self.assertTrue(auto_publish.is_twitch_candidate(channel, valid, set()))
        self.assertFalse(auto_publish.is_twitch_candidate(channel, valid, {"777"}))
        self.assertFalse(auto_publish.is_twitch_candidate(channel, {**valid, "type": "upload"}, set()))
        self.assertFalse(auto_publish.is_twitch_candidate(channel, {**valid, "duration": "59m59s"}, set()))

    def test_twitch_discovery_can_require_a_matchup_and_limit_duration(self):
        channel = {"includeTitle": r"\bVALORANT\b", "excludeTitle": r"\bPREP\b", "minimumDuration": 3600,
                   "maximumDuration": 36000, "requireMatchup": True}
        valid = {"id": "1234567890", "stream_id": "777", "type": "archive",
                 "title": "LOUD vs EDG - VALORANT watch party", "duration": "6h"}
        self.assertTrue(auto_publish.is_twitch_candidate(channel, valid, set()))
        self.assertFalse(auto_publish.is_twitch_candidate(channel, {**valid, "title": "$50K VALORANT TOURNEY PREP"}, set()))
        self.assertFalse(auto_publish.is_twitch_candidate(channel, {**valid, "duration": "11h"}, set()))

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
        self.assertTrue(auto_publish.should_attempt(key, set(), {key: {"status": "published"}}, 6, now))
        self.assertFalse(auto_publish.should_attempt(key, set(), {key: {"status": "superseded"}}, 6, now))

    def test_detector_holds_wait_for_a_pipeline_update(self):
        now = datetime.now(timezone.utc)
        key = "twitch:1234567890"
        held = {key: {"status": "held", "message": "The first detected round is not round 1. Check the beginning of this recording.",
                      "checkedAt": (now - timedelta(days=2)).isoformat(), "detectorVersion": auto_publish.DETECTOR_VERSION,
                      "pipelineVersion": auto_publish.PIPELINE_VERSION, "retryClass": "pipeline-update"}}
        self.assertFalse(auto_publish.should_attempt(key, set(), held, 6, now))
        held[key]["pipelineVersion"] = "older"
        self.assertTrue(auto_publish.should_attempt(key, set(), held, 6, now))

    def test_manual_retry_bypasses_held_cooldown_without_republishing(self):
        now = datetime.now(timezone.utc)
        key = "youtube:abcdefghijk"
        state = {key: {"status": "held", "checkedAt": now.isoformat(),
                       "pipelineVersion": auto_publish.PIPELINE_VERSION, "retryClass": "pipeline-update"}}
        self.assertFalse(auto_publish.should_process(key, set(), state, 6, now))
        self.assertTrue(auto_publish.should_process(key, set(), state, 6, now, True))
        self.assertFalse(auto_publish.should_process(key, {key}, state, 6, now, True))
        self.assertFalse(auto_publish.should_process("youtube:lmnopqrstuv", set(), state, 6, now, True))

    def test_pipeline_summary_reports_health_without_spoiler_data(self):
        state = {"videos": {
            "twitch:1234567890": {"status": "published", "channel": "Official", "checkedAt": "2026-09-29T12:00:00Z",
                                    "message": "Published"},
            "youtube:abcdefghijk": {"status": "held", "channel": "YouTube", "checkedAt": "2026-09-29T13:00:00Z",
                                    "message": "No match | retry later"}}}
        summary = auto_publish.pipeline_summary(state)
        self.assertIn("Published: **1**", summary)
        self.assertIn("Held: **1**", summary)
        self.assertIn("youtube:abcdefghijk", summary)
        self.assertIn("No match \\| retry later", summary)
        self.assertNotIn("rounds", summary.lower())

    def test_two_new_youtube_matches_can_be_processed_in_one_run(self):
        channel = {"provider": "youtube", "name": "YouTube", "priority": 1}
        config = {"channels": [channel], "lookback": 30, "maxPerRun": 4, "maxPerChannelPerRun": 2}
        state = {"videos": {}}
        catalog = {"videos": []}
        entries = [{"id": "abcdefghijk", "title": "A vs B - FULL MATCH"},
                   {"id": "lmnopqrstuv", "title": "C vs D - FULL MATCH"}]
        with patch.object(auto_publish, "read_json", side_effect=[config, state, catalog]), \
                patch.object(auto_publish, "discover_youtube", return_value=entries), \
                patch.object(auto_publish, "process", return_value=(True, "Published")) as process, \
                patch.object(auto_publish, "write_json"), patch.object(sys, "argv", ["auto_publish.py"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(auto_publish.main(), 0)
        self.assertEqual([call.args[1]["id"] for call in process.call_args_list], ["abcdefghijk", "lmnopqrstuv"])

    def test_stale_retries_are_limited_without_reducing_fresh_capacity(self):
        now = datetime.now(timezone.utc)
        channel = {"provider": "youtube", "name": "YouTube"}
        config = {"channels": [channel], "lookback": 30, "maxPerRun": 4,
                  "maxPerChannelPerRun": 4, "maxRetriesPerRun": 1, "youtubeRetryHours": 0}
        entries = [{"id": "abcdefghijk", "title": "Fresh"},
                   {"id": "lmnopqrstuv", "title": "Retry one"},
                   {"id": "12345678901", "title": "Retry two"}]
        state = {"videos": {
            "youtube:lmnopqrstuv": {"status": "held", "message": "network", "checkedAt": now.isoformat(),
                                     "detectorVersion": auto_publish.DETECTOR_VERSION,
                                     "pipelineVersion": auto_publish.PIPELINE_VERSION},
            "youtube:12345678901": {"status": "held", "message": "network", "checkedAt": now.isoformat(),
                                     "detectorVersion": auto_publish.DETECTOR_VERSION,
                                     "pipelineVersion": auto_publish.PIPELINE_VERSION}}}
        with patch.object(auto_publish, "read_json", side_effect=[config, state, {"videos": []}]), \
                patch.object(auto_publish, "discover_youtube", return_value=entries), \
                patch.object(auto_publish, "process", return_value=(True, "Published")) as process, \
                patch.object(auto_publish, "write_json"), patch.object(sys, "argv", ["auto_publish.py"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(auto_publish.main(), 0)
        self.assertEqual([call.args[1]["id"] for call in process.call_args_list], ["abcdefghijk", "lmnopqrstuv"])

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

    def test_youtube_publication_supersedes_its_official_twitch_source(self):
        channel = {"provider": "youtube", "name": "YouTube", "minimumDuration": 3600}
        entry = {"id": "abcdefghijk", "title": "A vs B - FULL MATCH"}
        state = {"videos": {"twitch:1234567890": {"status": "published", "message": "Published",
                                                    "publishedAt": "2026-09-29T10:00:00Z"}}}
        rounds = [{"map": 1, "round": number, "start": number * 100} for number in range(1, 14)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            (site / "indexes").mkdir(parents=True)
            (site / "catalog.json").write_text('{"version":2,"videos":[{"provider":"twitch","sourceId":"1234567890"}]}')
            with patch.object(auto_publish, "SITE", site), patch.object(auto_publish.server, "DATA", root / "data"), \
                    patch.object(auto_publish.server, "save"), \
                    patch.object(auto_publish, "youtube_alignment", return_value=("1234567890", {"offset": 10}, rounds)):
                self.assertEqual(auto_publish.process(channel, entry, {"minimumConfidence": 0.65, "minimumRounds": 13},
                                                      state, object()), (True, "Published"))
            catalog = auto_publish.read_json(site / "catalog.json")
        self.assertEqual([item["sourceId"] for item in catalog["videos"]], ["abcdefghijk"])
        self.assertEqual(catalog["videos"][0]["playedAt"], "2026-09-29T10:01:50Z")
        self.assertEqual(state["videos"]["twitch:1234567890"]["status"], "superseded")
        self.assertEqual(state["videos"]["twitch:1234567890"]["supersededBy"], "youtube:abcdefghijk")

    def test_two_youtube_matches_can_reuse_one_superseded_twitch_broadcast(self):
        youtube_channel = {"provider": "youtube", "name": "YouTube", "minimumDuration": 3600}
        config = {"channels": [youtube_channel, {"provider": "twitch", "name": "Official",
                                                 "alignmentSource": True}],
                  "alignmentLookback": 8, "minimumConfidence": 0.65, "minimumRounds": 13}
        state = {"videos": {"twitch:1234567890": {"status": "published", "channel": "Official",
                                                    "publishedAt": "2026-09-29T10:00:00Z"}}}
        rounds = [{"map": 1, "round": number, "start": number * 100} for number in range(1, 14)]
        entries = [{"id": "abcdefghijk", "title": "A vs B - FULL MATCH"},
                   {"id": "lmnopqrstuv", "title": "C vs D - FULL MATCH"}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            (site / "indexes").mkdir(parents=True)
            (site / "indexes" / "twitch-1234567890.json").write_text('{"rounds":[]}', encoding="utf-8")
            (site / "catalog.json").write_text(
                '{"version":2,"videos":[{"provider":"twitch","sourceId":"1234567890"}]}', encoding="utf-8")
            with patch.object(auto_publish, "SITE", site), patch.object(auto_publish.server, "DATA", root / "data"), \
                    patch.object(auto_publish.server, "save"), \
                    patch.object(auto_publish, "storyboard", side_effect=lambda provider, identifier, _: {
                        "duration": 5000, "provider": provider, "sourceId": identifier}), \
                    patch.object(auto_publish, "align_storyboards", return_value={"anchors": 20, "offset": 10}), \
                    patch.object(auto_publish, "translate_index", return_value=rounds):
                for entry in entries:
                    self.assertEqual(auto_publish.process(youtube_channel, entry, config, state, object()),
                                     (True, "Published"))
            catalog = auto_publish.read_json(site / "catalog.json")
        self.assertEqual([item["sourceId"] for item in catalog["videos"]], ["lmnopqrstuv", "abcdefghijk"])
        self.assertEqual(state["videos"]["twitch:1234567890"]["status"], "superseded")
        self.assertEqual(state["videos"]["twitch:1234567890"]["supersededBy"], "youtube:lmnopqrstuv")

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
        self.assertEqual([call.args[1]["id"] for call in process.call_args_list], ["1234567891", "1234567890"])
        self.assertEqual(state["videos"]["twitch:1234567890"]["status"], "published")
        self.assertIn("YouTube discovery failed", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
