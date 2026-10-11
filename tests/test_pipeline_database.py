import importlib.util
import json
import hashlib
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

if importlib.util.find_spec("psycopg") is None:
    raise unittest.SkipTest("Install indexer/requirements-worker.txt to run persistent pipeline tests")

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))

from pipeline.config import Config
from pipeline.deployment import Deployment
from pipeline.cli import main as cli_main, release_to_site
from pipeline.coordinator import Coordinator
from pipeline.detection import DETECTOR_VERSION, CandidateDetector
from pipeline.errors import ContinueJob, NeedsReview, StaleAttempt, WaitingSource, WaitingWork
from pipeline.processing import Processing
from pipeline.publishing import export_snapshot, persist_index
from pipeline.schedule import ingest_schedule
from pipeline.storage import LocalStorage
from pipeline.media import MediaAnalysis
from pipeline.store import Store, identifier, json_bytes
from pipeline.twitch import TwitchCapture, ingest_event
from pipeline.worker import Worker
from pipeline.youtube import timestamp
from test_pipeline import fingerprints, series


DATABASE = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(DATABASE, "TEST_DATABASE_URL must point to a disposable PostgreSQL database")
class DatabasePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not conninfo_to_dict(DATABASE).get("dbname", "").endswith("_test"):
            raise ValueError("Integration tests require a disposable database name ending in _test")
        cls.store = Store(DATABASE)
        cls.store.migrate()

    def setUp(self):
        self.store.execute(
            "TRUNCATE pipeline.broadcasts,pipeline.expected_matches,pipeline.jobs,pipeline.source_events,pipeline.deployments CASCADE"
        )
        self.broadcast = identifier()
        self.source = identifier()
        self.match = identifier()
        self.segment = identifier()
        self.store.execute(
            """INSERT INTO pipeline.broadcasts(id,event,day,region,channel_id,youtube_id,state,actual_start,seekable)
                           VALUES (%s,'Champions','2026-10-04','international','official','abcdefghijk','live','2026-10-04T10:00:00Z',true)""",
            (self.broadcast,),
        )
        self.store.execute(
            "INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,actual_start) VALUES (%s,%s,'youtube','abcdefghijk','canonical','2026-10-04T10:00:00Z')",
            (self.source, self.broadcast),
        )
        self.store.execute(
            "UPDATE pipeline.sources SET metadata=%s WHERE id=%s",
            (Jsonb({"live_dvr_range": {"start": 0, "end": 100000, "revision": 1, "playback_shift": 0}}), self.source),
        )
        self.store.execute(
            """INSERT INTO pipeline.expected_matches(id,broadcast_id,event,stage,day,team_a,team_b,match_order,best_of,region,channel_id,provider,external_id,completion)
                           VALUES (%s,%s,'Champions','Groups','2026-10-04','A','B',1,1,'international','official','manual','match','completed')""",
            (self.match, self.broadcast),
        )
        self.store.execute(
            """INSERT INTO pipeline.segments(id,broadcast_id,expected_match_id,generation,start_time,end_time,state,rounds)
                           VALUES (%s,%s,%s,1,100,1300,'validating',%s)""",
            (self.segment, self.broadcast, self.match, Jsonb(series())),
        )
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Config(DATABASE, storage_path=Path(self.temporary.name))
        self.storage = LocalStorage(self.temporary.name)

    def job(self, kind="validate", key=None, payload=None):
        self.store.enqueue(
            kind,
            key or str(identifier()),
            broadcast=self.broadcast,
            source=self.source,
            match=self.match,
            payload=payload or {"segment_id": str(self.segment)},
        )
        return self.store.claim()

    def entities(self):
        return (
            self.store.one("SELECT * FROM pipeline.segments WHERE id=%s", (self.segment,)),
            self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (self.source,)),
            self.store.one("SELECT * FROM pipeline.expected_matches WHERE id=%s", (self.match,)),
        )

    def test_completion_observation_and_publication_metrics_are_honest_and_stable(self):
        self.store.execute("UPDATE pipeline.expected_matches SET completion='running' WHERE id=%s", (self.match,))
        entry = {'provider': 'manual', 'external_id': 'match', 'event': 'Champions', 'stage': 'Groups',
                 'day': '2026-10-04', 'team_a': 'A', 'team_b': 'B', 'match_order': 1, 'best_of': 1,
                 'region': 'international', 'channel_id': 'official', 'completion': 'completed', 'metadata': {}}
        provider = Mock()
        provider.matches.return_value = [entry]
        ingest_schedule(self.store, [provider])
        observed = self.store.one('SELECT completion_observed_at FROM pipeline.expected_matches WHERE id=%s', (self.match,))['completion_observed_at']
        self.assertIsNotNone(observed)
        ingest_schedule(self.store, [provider])
        self.assertEqual(self.store.one('SELECT completion_observed_at FROM pipeline.expected_matches WHERE id=%s', (self.match,))['completion_observed_at'], observed)
        entry['completion'] = 'running'
        ingest_schedule(self.store, [provider])
        entry['completion'] = 'completed'
        ingest_schedule(self.store, [provider])
        self.assertEqual(self.store.one('SELECT completion_observed_at FROM pipeline.expected_matches WHERE id=%s', (self.match,))['completion_observed_at'], observed)
        metrics = self.store.performance(self.broadcast)
        self.assertIsNone(metrics['matches'][0]['first_published_at'])
        self.store.execute("INSERT INTO pipeline.deployments(commit_sha,repository,branch,state,matches) VALUES (%s,'fixture','main','pushed',%s)", ('a' * 40, Jsonb([{'expectedMatchId': str(self.match), 'pipelineState': 'provisional'}])))
        self.assertIsNone(self.store.performance(self.broadcast)['matches'][0]['first_published_at'])
        self.store.execute("UPDATE pipeline.deployments SET state='deployed' WHERE commit_sha=%s", ('a' * 40,))
        metrics = self.store.performance(self.broadcast)
        self.assertIsNotNone(metrics['matches'][0]['first_provisional_at'])
        self.assertIsNone(metrics['matches'][0]['first_final_at'])
        self.assertGreaterEqual(metrics['matches'][0]['observed_publication_latency_seconds'], 0)

    def test_waiting_validation_does_not_reexport_unchanged_index(self):
        self.store.execute("UPDATE pipeline.expected_matches SET completion='running' WHERE id=%s", (self.match,))
        job = self.store.enqueue('validate', 'unchanged', broadcast=self.broadcast, source=self.source, match=self.match,
                                 payload={'segment_id': str(self.segment)})
        worker = Worker(self.store, self.config, self.storage, coordinator=Mock())
        worker.run_once()
        self.assertIsNone(self.store.one("SELECT id FROM pipeline.jobs WHERE kind='export'"))
        self.store.execute("UPDATE pipeline.expected_matches SET completion='completed' WHERE id=%s", (self.match,))
        self.store.execute('UPDATE pipeline.jobs SET available_at=now() WHERE id=%s', (job['id'],))
        worker.run_once()
        self.assertIsNotNone(self.store.one("SELECT id FROM pipeline.jobs WHERE kind='export'"))

    def test_stale_final_index_still_exports_withdrawal(self):
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1,reconciled_revision=1 WHERE id=%s", (self.broadcast,))
        job = self.job()
        segment, source, match = self.entities()
        index = persist_index(self.store, job, segment, source, match, series(), 'final', False, revision=1)
        self.store.finish(job)
        self.store.execute("UPDATE pipeline.segments SET revision=revision+1,findings=%s WHERE id=%s", (Jsonb([{'code': 'missing_rounds'}]), self.segment))
        self.store.enqueue('validate', 'invalidate-final', broadcast=self.broadcast, source=self.source, match=self.match,
                           payload={'segment_id': str(self.segment)})
        worker = Worker(self.store, self.config, self.storage, coordinator=Mock())
        worker.run_once()
        self.assertEqual(self.store.one('SELECT id FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1')['id'], index['id'])
        self.assertIsNotNone(self.store.one("SELECT id FROM pipeline.jobs WHERE kind='export'"))
        self.assertEqual(export_snapshot(self.store, self.storage, False, ['abcdefghijk'])['videos'], [])

    def test_automatic_deployment_is_idempotent_and_preserves_history(self):
        import subprocess

        root = Path(self.temporary.name)
        remote, seed = root / "remote.git", root / "seed"
        remote.mkdir()
        seed.mkdir()

        def git(directory, *arguments):
            return subprocess.run(["git", "-C", str(directory), *arguments], check=True, capture_output=True, text=True).stdout.strip()

        git(remote, "init", "--bare", "--initial-branch=main")
        git(seed, "init", "--initial-branch=main")
        (seed / "site").mkdir()
        (seed / "site/core.js").write_bytes((Path(__file__).resolve().parents[1] / "site/core.js").read_bytes())
        legacy = {"sourceId": "old-video", "title": "Historical match", "provider": "youtube"}
        (seed / "site/catalog.json").write_text(json.dumps({"version": 2, "videos": [legacy]}))
        git(seed, "add", "site")
        git(seed, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Seed")
        git(seed, "remote", "add", "origin", str(remote))
        git(seed, "push", "origin", "main")
        validation = self.job()
        persist_index(self.store, validation, *self.entities(), series(), "provisional", False)
        self.store.finish(validation)
        config = Config(DATABASE, shadow=False, storage_path=root, settings={
            "approved_broadcasts": ["abcdefghijk"], "deployment": {"repository": str(remote), "branch": "main"}})
        deployment = Deployment(self.store, config)
        for iteration in range(2):
            self.store.enqueue("deploy", "deploy:site", priority=50)
            job = self.store.claim()
            deployment.publish(job)
            self.store.finish(job)
        self.assertEqual(git(remote, "rev-list", "--count", "main"), "2")
        catalog = json.loads(git(remote, "show", "main:site/catalog.json"))
        self.assertIn(legacy, catalog["videos"])
        self.assertEqual(len(catalog["videos"]), 2)
        self.assertEqual(catalog["videos"][0]["pipelineState"], "provisional")
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.deployments")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.jobs WHERE kind='verify_deployment'")), 1)
        self.assertEqual(self.store.one("SELECT checkpoint_time FROM pipeline.sources WHERE id=%s", (self.source,))["checkpoint_time"], -1)
        self.store.execute("UPDATE pipeline.segments SET state='needs_review' WHERE id=%s", (self.segment,))
        self.store.enqueue("deploy", "deploy:site", priority=50)
        job = self.store.claim()
        deployment.publish(job)
        self.store.finish(job)
        catalog = json.loads(git(remote, "show", "main:site/catalog.json"))
        self.assertEqual(catalog["videos"], [legacy])
        self.assertEqual(len(catalog["withheld"]), 1)

    def test_operator_status_excludes_spoilers_and_raw_errors(self):
        self.store.execute("UPDATE pipeline.expected_matches SET day=current_date WHERE id=%s", (self.match,))
        self.store.execute("UPDATE pipeline.segments SET findings=%s WHERE id=%s",
                           (Jsonb([{"code": "missing_rounds", "map": 3, "scores": [12, 5]}]), self.segment))
        job = self.job()
        self.store.finish(job, "needs_review", "Spoiler-containing raw diagnostic")
        status = self.store.operator_status(Config(DATABASE, settings={"deployment": {"repository": "https://github.com/a/b.git"}}))
        match = next(row for row in status["matches"] if row["id"] == self.match)
        self.assertTrue(match["needs_review"])
        self.assertEqual(match["state"], "validating")
        self.assertEqual((match["team_a"], match["team_b"]), ("A", "B"))
        self.assertEqual(match["checks"], ["Some rounds are missing from the recording."])
        self.assertEqual(match["tasks"][0]["kind"], "validate")
        self.assertEqual(match["tasks"][0]["state"], "needs_review")
        self.assertIsNone(match["tasks"][0]["reason"])
        text = json.dumps(status, default=str)
        for forbidden in ("scores", "findings", "checkpoint_time", "Spoiler-containing"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(status["budget"]["used"], 0)
        self.assertTrue(any(row["provider"] == "youtube" for row in status["captures"]))

    def test_operator_status_orders_latest_matches_first(self):
        self.store.execute("UPDATE pipeline.expected_matches SET day=current_date-1 WHERE id=%s", (self.match,))
        for day, order in (("current_date", 1), ("current_date", 2), ("current_date-1", 2)):
            self.store.execute(
                f"""INSERT INTO pipeline.expected_matches(id,broadcast_id,event,stage,day,team_a,team_b,
                    match_order,best_of,region,channel_id,provider,external_id,completion)
                    SELECT %s,broadcast_id,event,stage,{day},team_a,team_b,%s,best_of,region,channel_id,
                    provider,%s,completion FROM pipeline.expected_matches WHERE id=%s""",
                (identifier(), order, f"{day}-{order}", self.match))
        matches = self.store.operator_status(self.config)["matches"]
        self.assertEqual([row["match_order"] for row in matches], [2, 1, 2, 1])
        self.assertGreater(matches[0]["day"], matches[2]["day"])

    def test_operator_activity_reports_safe_progress_for_older_running_broadcasts(self):
        job = self.job("twitch_align", payload={"activity": {"phase": "searching", "completed": 4, "total": 13,
                        "match_id": str(self.match), "map": 1, "round": 5, "scores": [4, 0], "raw": "private diagnostic"},
                        "scoreboard_checks": {"checks": {"secret": {"scores": [4, 0]}}}})
        status = self.store.operator_status(self.config)
        self.assertEqual(status["matches"], [])
        item = status["activity"][0]
        self.assertEqual(item["id"], job["id"])
        self.assertEqual((item["team_a"], item["team_b"]), ("A", "B"))
        self.assertEqual(item["phase"], "searching")
        self.assertEqual(item["progress"], {"completed": 4, "total": 13, "map": 1, "round": 5})
        self.assertIsNotNone(item["started_at"])
        serialized = json.dumps(item, default=str)
        self.assertNotIn("scores", serialized)
        self.assertNotIn("private diagnostic", serialized)
        self.store.finish(job)
        self.assertEqual(self.store.operator_status(self.config)["activity"], [])

    def test_operator_status_explains_upload_checks_and_shared_work(self):
        self.store.execute("UPDATE pipeline.expected_matches SET day=current_date WHERE id=%s", (self.match,))
        self.store.execute("UPDATE pipeline.segments SET evidence=%s,findings='[]',state='final' WHERE id=%s",
                           (Jsonb({"full_match_validation": {"source_findings": [{"code": "incomplete_series", "scores": [13, 2]}]}}), self.segment))
        upload = self.job("validate_upload")
        self.store.finish(upload, "needs_review", "Full-match OCR itself requires review: private scores and paths")
        archive = self.job("fingerprint_archive")
        self.store.execute("UPDATE pipeline.jobs SET expected_match_id=NULL WHERE id=%s", (archive["id"],))
        obsolete = self.store.enqueue("recover", "obsolete-recovery", broadcast=self.broadcast, match=self.match)
        self.store.execute("UPDATE pipeline.jobs SET generation=0,state='needs_review' WHERE id=%s", (obsolete["id"],))
        match = self.store.operator_status(self.config)["matches"][0]
        self.assertEqual(match["state"], "final")
        self.assertEqual(len(match["tasks"]), 2)
        upload_task = next(task for task in match["tasks"] if task["kind"] == "validate_upload")
        self.assertEqual(upload_task["reason"], "Text recognition in the separate full-match upload needs checking.")
        self.assertEqual(upload_task["checks"], ["The recording does not confirm a complete match."])
        self.assertEqual(upload_task["scope"], "match")
        archive_task = next(task for task in match["tasks"] if task["kind"] == "fingerprint_archive")
        self.assertEqual(archive_task["state"], "running")
        self.assertEqual(archive_task["scope"], "broadcast")
        self.assertNotIn("private scores", json.dumps(match, default=str))
        self.assertEqual(upload_task["id"], upload["id"])
        self.assertEqual(upload_task["sources"][0]["url"], "https://www.youtube.com/watch?v=abcdefghijk")

    def test_operator_review_retry_preserves_validation_and_rejects_stale_tasks(self):
        job = self.job()
        with self.assertRaises(ValueError):
            self.store.retry_job(job["id"], review_only=True)
        self.store.finish(job, "needs_review", "Needs checking")
        self.store.execute("UPDATE pipeline.jobs SET failure_count=3 WHERE id=%s", (job["id"],))
        self.store.retry_job(job["id"], review_only=True)
        retried = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
        self.assertEqual(retried["state"], "queued")
        self.assertEqual(retried["failure_count"], 0)
        self.assertIsNone(retried["last_error"])
        self.assertEqual(retried["payload"], job["payload"])
        self.assertEqual(self.store.one("SELECT state FROM pipeline.segments WHERE id=%s", (self.segment,))["state"], "validating")
        with self.assertRaises(ValueError):
            self.store.retry_job(job["id"], review_only=True)
        self.store.execute("UPDATE pipeline.jobs SET state='needs_review',generation=0 WHERE id=%s", (job["id"],))
        with self.assertRaises(ValueError):
            self.store.retry_job(job["id"], review_only=True)
        with self.assertRaises(ValueError):
            self.store.retry_job(identifier(), review_only=True)

    def test_deployment_budget_counts_rolling_publications_and_expires(self):
        settings = {"repository": "https://github.com/a/b.git", "branch": "main"}
        deployment = Deployment(self.store, self.config)
        for number in range(20):
            self.store.execute(
                "INSERT INTO pipeline.deployments(commit_sha,repository,branch,state,publication_kind) VALUES (%s,%s,%s,'pushed',%s)",
                (f"{number:040x}", settings["repository"], settings["branch"], "incremental" if number < 4 else "ready"))
        with self.store.transaction() as connection:
            for kind in ("ready", "incremental"):
                with self.assertRaises(WaitingWork):
                    deployment.check_budget(connection, settings, kind)
        self.store.execute("UPDATE pipeline.deployments SET created_at=now()-interval '25 hours' WHERE publication_kind='ready'")
        with self.store.transaction() as connection:
            deployment.check_budget(connection, settings, "ready")
            with self.assertRaises(WaitingWork):
                deployment.check_budget(connection, settings, "incremental")
        self.store.execute("UPDATE pipeline.deployments SET created_at=now()-interval '25 hours'")
        with self.store.transaction() as connection:
            deployment.check_budget(connection, settings, "incremental")

    def test_stale_deployment_attempt_cannot_push(self):
        job = self.job("deploy")
        self.store.execute("UPDATE pipeline.jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (job["id"],))
        config = Config(DATABASE, shadow=False, storage_path=Path(self.temporary.name), settings={
            "approved_broadcasts": ["abcdefghijk"], "deployment": {"repository": "https://github.com/a/b.git"}})
        deployment = Deployment(self.store, config)
        with patch.object(deployment, "git", return_value="") as git:
            with self.assertRaises(StaleAttempt):
                deployment.publish(job)
        self.assertFalse(any("push" in call.args for call in git.call_args_list))
        self.assertEqual(self.store.rows("SELECT * FROM pipeline.deployments"), [])

    def test_auto_approval_revalidates_stored_rounds_without_duplicate_jobs(self):
        capture = self.job("live")
        candidates = [{"accepted": True, "confidence": .99, "detector_version": "fixture", "media_time": value["start"],
                       "round_number": value["round"], "scores": value["scores"],
                       "evidence": {"start": value["start"], "broadcast_map": value["map"], "teams": value["teams"]}}
                      for value in series()]
        self.store.checkpoint(capture, self.entities()[1], 1300, {}, candidates, [])
        self.store.finish(capture)
        segmentation = self.job("segment")
        Processing(self.store, self.config, self.storage).segment(segmentation)
        self.store.finish(segmentation)
        validation = self.store.claim()
        Processing(self.store, self.config, self.storage).validate(validation)
        self.store.finish(validation)
        self.store.execute("UPDATE pipeline.broadcasts SET metadata=%s WHERE id=%s", (Jsonb({"channel_id": "official"}), self.broadcast))
        config = Config(DATABASE, shadow=False, settings={"auto_publish": [{"channel_id": "official", "event": "Champions", "from_day": "2026-10-04"}]})
        processing = Processing(self.store, config, self.storage)
        segmentation = self.job("segment")
        processing.segment(segmentation)
        processing.segment(segmentation)
        self.store.finish(segmentation)
        updated = self.store.claim()
        self.assertEqual(updated["id"], validation["id"])
        self.assertFalse(updated["payload"]["shadow"])
        processing.validate(updated)
        self.assertFalse(self.store.one("SELECT shadow FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")["shadow"])
        self.assertEqual(self.entities()[1]["checkpoint_time"], 1300)

    def test_twitch_archive_discovery_reuses_live_source_and_preserves_checkpoint(self):
        source_id = identifier()
        broadcast = self.store.one("SELECT actual_start FROM pipeline.broadcasts WHERE id=%s", (self.broadcast,))
        self.store.execute("UPDATE pipeline.broadcasts SET day=current_date,state='archive_ready' WHERE id=%s", (self.broadcast,))
        self.store.execute("""INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,state,actual_start,checkpoint_time,metadata)
                            VALUES (%s,%s,'twitch','live-stream','official_twitch','ended',%s,1200,%s)""",
                           (source_id, self.broadcast, broadcast["actual_start"], Jsonb({"login": "valorant"})))
        job = self.job("discover_twitch_vods", payload={"channel": {"login": "valorant", "role": "official_twitch",
                                                                   "official_youtube_channel_id": "official"}})
        twitch = TwitchCapture(self.store, self.config, Mock())
        with patch("yt_dlp.YoutubeDL") as downloader, patch("pipeline.twitch.resolve_twitch") as resolve:
            downloader.return_value.__enter__.return_value.extract_info.return_value = {"entries": [{"id": "v123456"}]}
            resolve.return_value = {"actual_start": broadcast["actual_start"], "duration": 10000}
            twitch.discover_vods(job)
            twitch.discover_vods(job)
            resolve.assert_not_called()
            self.store.finish(job)
            association = self.store.claim()
            self.assertEqual(association["kind"], "associate_twitch_vod")
            twitch.associate_vod(association)
            twitch.associate_vod(association)
        source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (source_id,))
        self.assertEqual(source["checkpoint_time"], 1200)
        self.assertEqual(source["metadata"]["vod_id"], "123456")
        self.assertEqual(len(self.store.rows("SELECT id FROM pipeline.sources WHERE provider='twitch'")), 1)
        self.assertEqual(len(self.store.rows("SELECT id FROM pipeline.jobs WHERE kind='chat_gaps'")), 1)
        self.assertEqual(len(self.store.rows("SELECT id FROM pipeline.jobs WHERE kind='twitch_vod'")), 1)

    def test_twitch_archive_ambiguous_broadcast_never_creates_source(self):
        broadcast = self.store.one("SELECT * FROM pipeline.broadcasts WHERE id=%s", (self.broadcast,))
        self.store.execute("UPDATE pipeline.broadcasts SET day=current_date WHERE id=%s", (self.broadcast,))
        self.store.execute("""INSERT INTO pipeline.broadcasts(id,event,day,region,channel_id,youtube_id,state,actual_start)
                           VALUES (%s,'Champions',current_date,'international','official','differentyt','archive_ready',%s)""",
                           (identifier(), broadcast["actual_start"] + timedelta(hours=1)))
        job = self.job("associate_twitch_vod", payload={"channel": {"login": "valorant", "role": "official_twitch",
                                                                   "official_youtube_channel_id": "official"}, "vod_id": "123456"})
        with patch("pipeline.twitch.resolve_twitch", return_value={"actual_start": broadcast["actual_start"], "duration": 10000}):
            with self.assertRaises(NeedsReview):
                TwitchCapture(self.store, self.config, Mock()).associate_vod(job)
        self.assertEqual(self.store.rows("SELECT id FROM pipeline.sources WHERE provider='twitch'"), [])

    def test_twitch_archive_association_preserves_manual_source_and_chat_role(self):
        source_id = identifier()
        start = self.store.one("SELECT actual_start FROM pipeline.broadcasts WHERE id=%s", (self.broadcast,))["actual_start"]
        self.store.execute("UPDATE pipeline.broadcasts SET day=current_date WHERE id=%s", (self.broadcast,))
        self.store.execute("""INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,actual_start,checkpoint_time,metadata)
                            VALUES (%s,%s,'twitch','123456','official_twitch',%s,600,%s)""",
                           (source_id, self.broadcast, start, Jsonb({"vod_id": "123456"})))
        job = self.job("associate_twitch_vod", payload={"channel": {"login": "valorant", "role": "official_twitch",
                                                                   "official_youtube_channel_id": "official"}, "vod_id": "123456"})
        twitch = TwitchCapture(self.store, self.config, Mock())
        with patch("pipeline.twitch.resolve_twitch", return_value={"actual_start": start, "duration": 10000}):
            twitch.associate_vod(job)
            self.assertEqual(self.store.one("SELECT checkpoint_time FROM pipeline.sources WHERE id=%s", (source_id,))["checkpoint_time"], 600)
            self.assertEqual(len(self.store.rows("SELECT id FROM pipeline.sources WHERE provider='twitch'")), 1)
            self.store.execute("UPDATE pipeline.sources SET role='watch_party' WHERE id=%s", (source_id,))
            with self.assertRaises(NeedsReview):
                twitch.associate_vod(job)

    def test_twitch_vod_registration_resume_and_chat_recovery_are_idempotent(self):
        arguments = ["pipeline.cli", "register-source", str(self.broadcast), "twitch", "123456789", "official_twitch"]
        with patch.dict(os.environ, {"DATABASE_URL": DATABASE}), patch.object(sys, "argv", arguments):
            cli_main()
            cli_main()
        twitch = self.store.one("SELECT * FROM pipeline.sources WHERE provider='twitch'")
        self.assertEqual(twitch["metadata"]["vod_id"], "123456789")
        self.assertEqual(self.store.one("SELECT count(*) AS total FROM pipeline.jobs WHERE kind='twitch_vod'")["total"], 1)
        canonical_before = self.entities()[1]
        media = Mock()
        media.archive_fingerprints.return_value = [{"time": 121, "hash": "1", "regions": ["1"]}]
        self.store.execute("UPDATE pipeline.jobs SET payload=%s WHERE kind='twitch_vod'", (Jsonb({"checkpoint": 120}),))
        job = self.store.claim()
        remote = {"url": "fixture", "duration": 360, "actual_start": timestamp("2026-10-04T10:00:00Z")}
        capture = TwitchCapture(self.store, self.config, Processing(self.store, self.config, self.storage), media)
        with patch("pipeline.twitch.resolve_twitch", return_value=remote), self.assertRaises(ContinueJob):
            capture.vod(job)
        media.archive_fingerprints.assert_called_once_with(remote, 120, 240, interval=2)
        self.assertEqual(self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]["checkpoint"], 240)
        self.assertEqual(self.entities()[1]["checkpoint_time"], canonical_before["checkpoint_time"])
        self.assertEqual(self.entities()[1]["detector_state"], canonical_before["detector_state"])
        archive_job = {**job, "source_id": twitch["id"]}

        def download_chat(arguments, **kwargs):
            Path(arguments[-1]).write_text(json.dumps({"comments": [
                {"_id": "inside", "content_offset_seconds": 200, "commenter": {"display_name": "Viewer"}, "message": {"body": "hello"}},
                {"_id": "outside", "content_offset_seconds": 400, "commenter": {"display_name": "Viewer"}, "message": {"body": "hello"}}
            ]}))
            return Mock(returncode=0)

        with patch.dict(os.environ, {"TWITCH_DOWNLOADER": "fixture"}), patch("subprocess.run", side_effect=download_chat):
            capture.chat_gaps(archive_job)
        self.assertEqual(self.store.one("SELECT count(*) AS total FROM pipeline.chat_messages WHERE source_id=%s", (twitch["id"],))["total"], 1)
        updated = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
        media.reset_mock()
        media.archive_fingerprints.return_value = [{"time": 241, "hash": "2"}]
        with patch("pipeline.twitch.resolve_twitch", return_value=remote):
            TwitchCapture(self.store, self.config, Processing(self.store, self.config, self.storage), media).vod(updated)
        media.archive_fingerprints.assert_called_once_with(remote, 240, 360, interval=2)
        self.store.execute("UPDATE pipeline.chat_archives SET state='complete' WHERE source_id=%s", (twitch["id"],))
        with patch("subprocess.run") as download:
            capture.chat_gaps({"source_id": twitch["id"]})
        download.assert_not_called()
        self.store.execute("UPDATE pipeline.jobs SET state='succeeded',lease_token=NULL,lease_until=NULL WHERE id=%s", (job["id"],))
        with patch.dict(os.environ, {"DATABASE_URL": DATABASE}), patch.object(sys, "argv", arguments):
            cli_main()
        stored = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
        self.assertEqual(stored["state"], "succeeded")
        self.assertEqual(stored["payload"]["checkpoint"], 360)

    def test_chat_download_does_not_block_canonical_indexing_or_overlap_its_source(self):
        twitch = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'twitch','123456789','official_twitch')", (twitch, self.broadcast))
        self.store.enqueue("chat_gaps", "chat", broadcast=self.broadcast, source=twitch, priority=10)
        chat = self.store.claim()
        self.assertEqual(chat["kind"], "chat_gaps")
        self.store.enqueue("twitch_vod", "twitch", broadcast=self.broadcast, source=twitch, priority=9)
        self.store.enqueue("live", "canonical", broadcast=self.broadcast, source=self.source, priority=8)
        canonical = self.store.claim()
        self.assertEqual(canonical["kind"], "live")
        self.assertIsNone(self.store.claim())
        self.store.finish(chat)
        self.assertEqual(self.store.claim()["kind"], "twitch_vod")
        self.store.finish(canonical)
        self.assertIsNone(self.store.claim())

    def test_twitch_archive_does_not_block_official_capture_but_keeps_other_work_serialized(self):
        for kind in ('live', 'fingerprint_archive', 'reconcile'):
            with self.subTest(kind=kind):
                self.store.execute("TRUNCATE pipeline.jobs CASCADE")
                twitch = identifier()
                self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'twitch',%s,'watch_party')", (twitch, self.broadcast, str(twitch)))
                archive = self.store.enqueue("twitch_vod", "archive", broadcast=self.broadcast, source=twitch)
                archive = self.store.claim(job_id=archive['id'])
                official = self.store.enqueue(kind, "official", broadcast=self.broadcast, source=self.source)
                official = self.store.claim(job_id=official['id'])
                self.assertIsNotNone(official)
                for blocked_kind, source in (('twitch_align', twitch), ('validate', self.source), ('live', self.source)):
                    blocked = self.store.enqueue(blocked_kind, blocked_kind, broadcast=self.broadcast, source=source)
                    self.assertIsNone(self.store.claim(job_id=blocked['id']))
                self.store.finish(archive)
                self.store.finish(official)

    def test_discovery_does_not_repeat_completed_alignment_for_unchanged_evidence(self):
        twitch = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,checkpoint_time) VALUES (%s,%s,'twitch','123456789','official_twitch',600)", (twitch, self.broadcast))
        coordinator = Coordinator(self.store, self.config)
        coordinator.discover()
        first = self.store.one("SELECT * FROM pipeline.jobs WHERE kind='twitch_align'")
        self.assertEqual(first["priority"], -40)
        self.store.execute("UPDATE pipeline.jobs SET state='succeeded' WHERE id=%s", (first["id"],))
        coordinator.discover()
        self.assertEqual(self.store.one("SELECT state FROM pipeline.jobs WHERE id=%s", (first["id"],))["state"], "succeeded")
        self.assertEqual(self.store.one("SELECT count(*) AS total FROM pipeline.jobs WHERE kind='twitch_align'")["total"], 1)
        self.store.execute("UPDATE pipeline.sources SET checkpoint_time=900 WHERE id=%s", (twitch,))
        coordinator.discover()
        self.assertEqual(self.store.one("SELECT count(*) AS total FROM pipeline.jobs WHERE kind='twitch_align'")["total"], 2)
        self.store.execute("UPDATE pipeline.broadcasts SET archive_revision=1,reconciled_revision=1 WHERE id=%s", (self.broadcast,))
        coordinator.discover()
        self.assertEqual(self.store.one("SELECT count(*) AS total FROM pipeline.jobs WHERE kind='twitch_align'")["total"], 3)

    def test_operational_failures_enter_bounded_cooldown_without_losing_progress(self):
        job = self.job("recover", payload={"checkpoint": 123})
        self.store.execute("UPDATE pipeline.jobs SET max_attempts=2 WHERE id=%s", (job["id"],))
        job["max_attempts"] = 2
        for attempt in range(6):
            self.store.finish(job, "waiting_source", "Temporary provider outage", failure_kind="network")
            row = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
            self.assertEqual(row["payload"]["checkpoint"], 123)
            self.assertEqual(row["failure_kind"], "network")
            if attempt in {1, 3}:
                self.assertEqual(row["state"], "waiting_source")
                self.assertIsNotNone(row["recovery_at"])
                self.assertEqual(row["recovery_count"], (attempt + 1) // 2)
                self.assertIsNone(self.store.claim(job_id=job["id"]))
            if attempt < 5:
                self.store.execute("UPDATE pipeline.jobs SET available_at=now() WHERE id=%s", (job["id"],))
                job = self.store.claim(job_id=job["id"])
        self.assertEqual(row["state"], "needs_review")
        self.store.retry_job(row["id"])
        reset = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (row["id"],))
        self.assertEqual(reset["recovery_count"], 0)
        self.assertIsNone(reset["failure_kind"])

    def test_graceful_interruption_requeues_without_a_failed_attempt(self):
        self.store.enqueue("recover", "interrupt", broadcast=self.broadcast, source=self.source, payload={"checkpoint": 123})
        processing = Mock()
        processing.recover.side_effect = InterruptedError("Worker stopping")
        Worker(self.store, self.config, self.storage, coordinator=Mock(), processing=processing).run_once()
        job = self.store.one("SELECT * FROM pipeline.jobs WHERE dedupe_key='interrupt'")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["failure_count"], 0)
        self.assertEqual(job["failure_kind"], "interrupted")
        self.assertEqual(job["payload"]["checkpoint"], 123)

    def test_dependency_waits_have_a_persistent_deadline_and_reset_on_manual_retry(self):
        job = self.job("twitch_align", payload={"dependency_wait_started": "2000-01-01T00:00:00+00:00"})
        self.store.finish(job, "waiting_source", "Awaiting canonical evidence", dependency=True, failure_kind="dependency")
        row = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
        self.assertEqual(row["state"], "needs_review")
        self.assertEqual(row["failure_kind"], "dependency_exhausted")
        self.assertIn("14-day recovery window", row["last_error"])
        self.store.retry_job(job["id"])
        self.assertNotIn("dependency_wait_started", self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"])

    def test_review_from_an_obsolete_generation_is_retired_but_remains_in_history(self):
        job = self.job("validate")
        self.store.finish(job, "needs_review", "Original invalid round sequence", failure_kind="data_quality")
        self.store.execute("UPDATE pipeline.broadcasts SET generation=generation+1 WHERE id=%s", (self.broadcast,))
        self.assertIsNone(self.store.claim())
        row = self.store.one("SELECT state,failure_kind FROM pipeline.jobs WHERE id=%s", (job["id"],))
        self.assertEqual(row, {"state": "unsupported", "failure_kind": "superseded"})
        attempt = self.store.one("SELECT state,error FROM pipeline.attempts WHERE job_id=%s", (job["id"],))
        self.assertEqual(attempt, {"state": "needs_review", "error": "Original invalid round sequence"})

    def test_deployment_quota_retry_is_reserved_and_counted_without_duplicate_requests(self):
        from datetime import datetime, timezone

        settings = {"repository": "https://github.com/a/b.git", "branch": "main", "daily_deployment_limit": 20}
        config = Config(DATABASE, shadow=False, storage_path=Path(self.temporary.name), settings={"deployment": settings})
        commit = "a" * 40
        self.store.execute("INSERT INTO pipeline.deployments(commit_sha,repository,branch,state) VALUES (%s,%s,'main','pushed')", (commit, settings["repository"]))
        job = self.job("verify_deployment", payload={"commit": commit})
        record = self.store.one("SELECT * FROM pipeline.deployments WHERE commit_sha=%s", (commit,))
        run = {"id": 123, "run_attempt": 1, "updated_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), "html_url": "https://github.com/a/b/actions/runs/123"}
        def response(url, headers):
            if "/contents/" in url:
                import base64

                workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/deploy-site.yml").read_bytes()
                return json.dumps({"content": base64.b64encode(workflow).decode()}).encode()
            if "/commits/" in url:
                return json.dumps({"sha": commit}).encode()
            if url.endswith("/jobs"):
                return json.dumps({"jobs": [{"id": 7, "conclusion": "failure"}]}).encode()
            return b"Resource is limited - try again in 24 hours api-deployments-free-per-day"
        deployment = Deployment(self.store, config)
        with patch("pipeline.deployment.fetch_bytes", side_effect=response), patch("pipeline.deployment.urlopen") as post:
            with self.assertRaisesRegex(WaitingWork, "retry requested"):
                deployment.retry_failed(job, record, run, "a/b", {"Authorization": "Bearer fixture"})
            post.assert_called_once()
            request = post.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            with self.assertRaisesRegex(NeedsReview, "unconfirmed"):
                deployment.retry_failed(job, record, run, "a/b", {"Authorization": "Bearer fixture"})
            post.assert_called_once()
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.deployment_retries")), 1)
        self.assertEqual(self.store.operator_status(config)["budget"]["used"], 2)
        with self.store.transaction() as connection:
            deployment.check_budget(connection, {**settings, "daily_deployment_limit": 3}, "ready")
            with self.assertRaises(WaitingWork):
                deployment.check_budget(connection, {**settings, "daily_deployment_limit": 2}, "ready")

    def test_deployment_retry_never_reruns_obsolete_or_invalid_code(self):
        from datetime import datetime, timezone

        deployment = Deployment(self.store, self.config)
        self.store.execute("INSERT INTO pipeline.deployments(commit_sha,repository,branch,state) VALUES (%s,'https://github.com/a/b.git','main','pushed')", ("a" * 40,))
        job = self.job("verify_deployment", payload={"commit": "a" * 40})
        record = self.store.one("SELECT * FROM pipeline.deployments WHERE commit_sha=%s", ("a" * 40,))
        run = {"id": 123, "updated_at": datetime.now(timezone.utc).isoformat(), "html_url": "https://github.com/a/b/actions/runs/123"}
        with patch("pipeline.deployment.fetch_bytes", return_value=json.dumps({"sha": "b" * 40}).encode()), patch("pipeline.deployment.urlopen") as post:
            deployment.retry_failed(job, record, run, "a/b", {"Authorization": "Bearer fixture"})
            post.assert_not_called()
        self.assertEqual(self.store.one("SELECT state FROM pipeline.deployments WHERE commit_sha=%s", ("a" * 40,))["state"], "superseded")
        responses = [json.dumps({"sha": "a" * 40}).encode(), json.dumps({"jobs": [{"id": 7, "conclusion": "failure"}]}).encode(), b"Error: invalid JavaScript syntax"]
        with patch("pipeline.deployment.fetch_bytes", side_effect=responses), patch("pipeline.deployment.urlopen") as post:
            with self.assertRaisesRegex(NeedsReview, "non-transient"):
                deployment.retry_failed({}, record, run, "a/b", {"Authorization": "Bearer fixture"})
            post.assert_not_called()

    def test_watchparty_search_resumes_inside_a_round_without_repeating_samples(self):
        from detector import Observation

        twitch = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,metadata) VALUES (%s,%s,'twitch','123456789','watch_party',%s)",
                           (twitch, self.broadcast, Jsonb({"vod_id": "123456789"})))
        self.store.execute("UPDATE pipeline.broadcasts SET archive_revision=1,reconciled_revision=1 WHERE id=%s", (self.broadcast,))
        self.store.execute("""INSERT INTO pipeline.match_indexes(id,broadcast_id,expected_match_id,segment_id,canonical_source_id,generation,version,archive_revision,state,rounds,provenance)
            VALUES (%s,%s,%s,%s,%s,1,1,1,'final',%s,'{}')""",
            (identifier(), self.broadcast, self.match, self.segment, self.source, Jsonb(series()[:1])))
        self.store.enqueue("twitch_align", "partial-search", broadcast=self.broadcast, source=twitch)
        job = self.store.claim()
        broadcast = self.store.one("SELECT * FROM pipeline.broadcasts WHERE id=%s", (self.broadcast,))
        source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (twitch,))
        canonical = self.entities()[1]
        mapping = {"timelineScale": 1, "anchors": 10, "maximumResidual": 1, "segments": [
            {"sourceStart": 100, "sourceEnd": 800, "canonicalStart": 0, "canonicalEnd": 700,
             "offset": -100, "anchors": 10, "maximumResidual": 1}]}
        starts = []
        def window(remote, lower, upper):
            starts.append(lower)
            for second in range(-7, 7):
                timestamp = 200 + second
                if lower <= timestamp < upper:
                    yield Observation(timestamp, 1, 100 - second if second >= 0 else 0, .99, scores=(0, 0), buy_phase=second < 0), {}, None
        media = Mock()
        media.watchparty_window.side_effect = window
        capture = TwitchCapture(self.store, self.config, Processing(self.store, self.config, self.storage), media=media)
        with patch("pipeline.twitch.resolve_twitch", return_value={"duration": 1000}), patch("pipeline.twitch.time.monotonic", side_effect=[0, 50]):
            with self.assertRaisesRegex(ContinueJob, "time slice"):
                capture.check_rounds(job, broadcast, source, canonical, mapping, 1)
        job = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
        self.assertEqual(job["payload"]["scoreboard_search"]["samples"][0]["time"], 193)
        with patch("pipeline.twitch.resolve_twitch", return_value={"duration": 1000}):
            result = capture.check_rounds(job, broadcast, source, canonical, mapping, 1)
        self.assertEqual(starts, [192, 194])
        self.assertEqual(next(iter(result["roundChecks"].values()))["status"], "verified")
        self.assertEqual(self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]["scoreboard_search"], {})

    def test_optional_alignment_does_not_block_official_work_and_rejects_stale_sources(self):
        twitch = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'twitch','123456789','watch_party')", (twitch, self.broadcast))
        self.store.enqueue("twitch_align", "optional-align", broadcast=self.broadcast, source=twitch)
        self.store.enqueue("validate", "official-validate", broadcast=self.broadcast, source=self.source)
        optional_id = self.store.one("SELECT id FROM pipeline.jobs WHERE dedupe_key='optional-align'")["id"]
        required_id = self.store.one("SELECT id FROM pipeline.jobs WHERE dedupe_key='official-validate'")["id"]
        job = self.store.claim(job_id=optional_id)
        required = self.store.claim(job_id=required_id)
        self.assertIsNotNone(required)
        source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (twitch,))
        canonical = self.entities()[1]
        self.store.execute("UPDATE pipeline.sources SET revision=revision+1 WHERE id=%s", (twitch,))
        with self.assertRaisesRegex(WaitingWork, "evidence changed"):
            Processing(self.store, self.config, self.storage).save_alignment(job, source, canonical, {}, "secondary", 0)
        self.assertEqual(self.store.rows("SELECT id FROM pipeline.alignments"), [])

    def test_chat_gap_recovery_resumes_bounded_downloads_after_network_failure(self):
        twitch = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,actual_start,metadata) VALUES (%s,%s,'twitch','123456789','watch_party','2026-10-04T10:00:00Z',%s)",
                           (twitch, self.broadcast, Jsonb({"vod_id": "123456789"})))
        self.store.execute("INSERT INTO pipeline.chat_archives(source_id,state,gaps) VALUES (%s,'waiting_vod',%s)", (twitch, Jsonb([[0, 2000]])))
        self.store.enqueue("chat_gaps", "bounded-chat", broadcast=self.broadcast, source=twitch)
        job = self.store.claim()
        capture = TwitchCapture(self.store, self.config, Processing(self.store, self.config, self.storage))
        calls = []
        def download(arguments, **kwargs):
            lower = float(arguments[arguments.index("--beginning") + 1][:-1])
            upper = float(arguments[arguments.index("--ending") + 1][:-1])
            calls.append([lower, upper])
            self.assertLessEqual(upper - lower, 900)
            self.assertEqual(kwargs["timeout"], 300)
            Path(arguments[-1]).write_text(json.dumps({"comments": [{"_id": str(lower), "content_offset_seconds": lower + 10,
                "commenter": {"display_name": "viewer"}, "message": {"body": "hello", "fragments": [{"text": "hello"}]}}]}))
            return Mock(returncode=0)
        with patch.dict(os.environ, {"TWITCH_DOWNLOADER": "downloader"}), patch("subprocess.run", side_effect=download):
            with self.assertRaises(ContinueJob):
                capture.chat_gaps(job)
        job = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
        self.assertEqual(job["payload"]["chat_coverage"], [[0, 900]])
        with patch.dict(os.environ, {"TWITCH_DOWNLOADER": "downloader"}), patch("subprocess.run", side_effect=TimeoutError("provider stalled")):
            with self.assertRaises(TimeoutError):
                capture.chat_gaps(job)
        self.assertEqual(self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]["chat_coverage"], [[0, 900]])
        with patch.dict(os.environ, {"TWITCH_DOWNLOADER": "downloader"}), patch("subprocess.run", side_effect=download):
            with self.assertRaises(ContinueJob):
                capture.chat_gaps(job)
            job = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
            capture.chat_gaps(job)
        self.assertEqual(calls, [[0, 900], [900, 1800], [1800, 2000]])
        self.assertEqual(self.store.one("SELECT state FROM pipeline.chat_archives WHERE source_id=%s", (twitch,))["state"], "complete")
        self.assertEqual(len(self.store.rows("SELECT message_id FROM pipeline.chat_messages WHERE source_id=%s", (twitch,))), 3)
        self.store.execute("UPDATE pipeline.chat_archives SET state='waiting_vod',gaps=%s WHERE source_id=%s", (Jsonb([[0, 2000], [2500, 2600]]), twitch))
        job = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
        with patch.dict(os.environ, {"TWITCH_DOWNLOADER": "downloader"}), patch("subprocess.run", side_effect=download):
            capture.chat_gaps(job)
        self.assertEqual(calls[-1], [2500, 2600])

    def test_canonical_checkpoint_yield_does_not_start_optional_alignment_in_delay_gap(self):
        self.store.enqueue("reconcile", "canonical", broadcast=self.broadcast, source=self.source, priority=30)
        self.store.enqueue("twitch_align", "optional", broadcast=self.broadcast, priority=-40)
        processing = Mock()
        processing.reconcile.side_effect = ContinueJob("Checkpointed")
        worker = Worker(self.store, self.config, self.storage, coordinator=Mock(), processing=processing)
        optional = Mock()
        worker.handlers["twitch_align"] = optional
        worker.run_once()
        worker.run_once()
        self.assertEqual(processing.reconcile.call_count, 2)
        optional.assert_not_called()
        self.assertEqual(self.store.one("SELECT state FROM pipeline.jobs WHERE dedupe_key='optional'")["state"], "queued")

    def test_twitch_local_alignment_preserves_unverified_intro_and_cut_gaps(self):
        twitch = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'twitch','123456789','official_twitch')", (twitch, self.broadcast))
        self.store.execute("UPDATE pipeline.broadcasts SET archive_revision=1,state='archive_ready' WHERE id=%s", (self.broadcast,))
        canonical = fingerprints(count=90, base=300, offsets=lambda index: 120 if index >= 45 else 0)
        target = fingerprints(count=90, base=100)
        target["frames"].insert(0, {"time": 0, "hash": "0" * 16})
        with self.store.transaction() as connection:
            for source, values in [(self.source, canonical), (twitch, target)]:
                for frame in values["frames"]:
                    connection.execute("INSERT INTO pipeline.fingerprints(source_id,source_revision,timeline,media_time,hashes) VALUES (%s,1,%s,%s,%s)", (source, "live" if source == self.source else "secondary", frame["time"], Jsonb({"hash": frame["hash"]})))
        self.store.enqueue("twitch_align", "alignment", broadcast=self.broadcast, source=twitch)
        job = self.store.claim()
        TwitchCapture(self.store, self.config, Processing(self.store, self.config, self.storage)).align(job)
        alignment = self.store.one("SELECT * FROM pipeline.alignments WHERE source_id=%s", (twitch,))
        self.assertEqual(alignment["canonical_revision"], 0)
        self.assertEqual(len(alignment["mapping"]["segments"]), 2)
        from pipeline.timeline import mapped_time
        with self.assertRaises(NeedsReview):
            mapped_time(0, alignment["mapping"])
        with self.assertRaises(NeedsReview):
            mapped_time(545, alignment["mapping"])

    def test_watchparty_scoreboard_checks_resume_and_reuse_verified_rounds(self):
        from detector import Observation

        twitch = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,metadata) VALUES (%s,%s,'twitch','123456789','watch_party',%s)",
                           (twitch, self.broadcast, Jsonb({"vod_id": "123456789"})))
        self.store.execute("UPDATE pipeline.broadcasts SET archive_revision=1,reconciled_revision=1,state='archive_ready' WHERE id=%s", (self.broadcast,))
        self.store.execute("""INSERT INTO pipeline.match_indexes(id,broadcast_id,expected_match_id,segment_id,canonical_source_id,generation,version,archive_revision,state,rounds,provenance)
                           VALUES (%s,%s,%s,%s,%s,1,1,1,'final',%s,'{}')""",
                           (identifier(), self.broadcast, self.match, self.segment, self.source, Jsonb(series()[:5])))
        self.store.enqueue("twitch_align", "scoreboard", broadcast=self.broadcast, source=twitch)
        job = self.store.claim()
        broadcast = self.store.one("SELECT * FROM pipeline.broadcasts WHERE id=%s", (self.broadcast,))
        source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (twitch,))
        canonical = self.entities()[1]
        mapping = {"timelineScale": 1, "anchors": 10, "maximumResidual": 1, "segments": [
            {"sourceStart": 100, "sourceEnd": 800, "canonicalStart": 0, "canonicalEnd": 700,
             "offset": -100, "anchors": 10, "maximumResidual": 1}]}
        media = Mock()
        def window(remote, lower, upper):
            number = round((lower + 8 - 200) / 100) + 1
            for second in range(-7, 7):
                yield Observation(200 + (number - 1) * 100 + second, number,
                                  100 - second if second >= 0 else 0, .99,
                                  scores=(number - 1, 0), buy_phase=second < 0), {}, None
        media.watchparty_window.side_effect = window
        processing = Processing(self.store, self.config, self.storage)
        capture = TwitchCapture(self.store, self.config, processing, media=media)
        with patch("pipeline.twitch.resolve_twitch", return_value={"duration": 1000}) as resolve:
            with self.assertRaisesRegex(ContinueJob, "4 of 5"):
                capture.check_rounds(job, broadcast, source, canonical, mapping, 1)
            job = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
            result = capture.check_rounds(job, broadcast, source, canonical, mapping, 1)
            self.assertEqual(media.watchparty_window.call_count, 5)
            self.assertEqual(len(result["roundChecks"]), 5)
            processing.save_alignment(job, source, canonical, result, "secondary", 1)
            job["payload"] = {}
            capture.check_rounds(job, broadcast, source, canonical, mapping, 1)
            self.assertEqual(resolve.call_count, 2)
            self.assertEqual(media.watchparty_window.call_count, 5)
        coordinator = Coordinator(self.store, self.config)
        self.store.execute("UPDATE pipeline.sources SET checkpoint_time=800 WHERE id=%s", (twitch,))
        coordinator.discover()
        coordinator.discover()
        self.assertEqual(self.store.one("SELECT count(*) AS total FROM pipeline.jobs WHERE source_id=%s AND dedupe_key LIKE '%%scoreboard-v2%%'", (twitch,))["total"], 1)
        self.store.execute("UPDATE pipeline.match_indexes SET version=2 WHERE expected_match_id=%s", (self.match,))
        coordinator.discover()
        self.assertEqual(self.store.one("SELECT count(*) AS total FROM pipeline.jobs WHERE source_id=%s AND dedupe_key LIKE '%%scoreboard-v2%%'", (twitch,))["total"], 2)
        self.store.execute("DELETE FROM pipeline.alignments WHERE source_id=%s", (twitch,))
        job["payload"] = {}
        media.watchparty_window.side_effect = lambda *args: (value for value in ())
        media.watchparty_window.reset_mock()
        with patch("pipeline.twitch.resolve_twitch", return_value={"duration": 1000}):
            with self.assertRaises(ContinueJob):
                capture.check_rounds(job, broadcast, source, canonical, mapping, 1)
            job = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
            result = capture.check_rounds(job, broadcast, source, canonical, mapping, 1)
        self.assertEqual(media.watchparty_window.call_count, 15)
        self.assertEqual(result["segments"], mapping["segments"])
        self.assertTrue(all(check["section"] is None for check in result["roundChecks"].values()))

    def test_watchparty_seed_verifies_short_window_without_full_video_scan(self):
        twitch = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,actual_start,metadata) VALUES (%s,%s,'twitch','123456789','watch_party','2026-10-04T10:00:00Z',%s)",
                           (twitch, self.broadcast, Jsonb({"vod_id": "123456789"})))
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1,reconciled_revision=1 WHERE id=%s", (self.broadcast,))
        self.store.execute("""INSERT INTO pipeline.match_indexes(id,broadcast_id,expected_match_id,segment_id,canonical_source_id,generation,version,archive_revision,state,rounds,provenance)
                           VALUES (%s,%s,%s,%s,%s,1,1,1,'final',%s,'{}')""",
                           (identifier(), self.broadcast, self.match, self.segment, self.source, Jsonb(series())))
        job = self.job("fingerprint_archive")
        canonical = self.entities()[1]
        frames = fingerprints(count=90, base=100, interval=2)["frames"]
        self.store.checkpoint(job, canonical, canonical["checkpoint_time"], {}, [], frames, timeline="archive", revision=1)
        self.store.finish(job)
        self.store.enqueue("twitch_vod", "seed-vod", broadcast=self.broadcast, source=twitch, priority=-30)
        job = self.store.claim()
        media = Mock()
        media.archive_fingerprints.side_effect = lambda remote, lower, upper, **kwargs: [value for value in frames if lower <= value["time"] < upper]
        remote = {"url": "fixture", "duration": 20000}
        capture = TwitchCapture(self.store, self.config, Processing(self.store, self.config, self.storage), media)
        with patch("pipeline.twitch.resolve_twitch", return_value=remote):
            capture.vod(job)
            updated = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
            capture.vod(updated)
        self.assertEqual(updated["payload"]["round_seed"]["state"], "verified")
        self.assertNotIn("checkpoint", updated["payload"])
        self.assertEqual(media.archive_fingerprints.call_count, 1)
        self.assertEqual(media.archive_fingerprints.call_args.args[1:3], (40, 220))
        alignment_job = self.store.one("SELECT * FROM pipeline.jobs WHERE kind='twitch_align' AND source_id=%s", (twitch,))
        self.assertTrue(alignment_job["payload"]["visual_alignment"]["mapping"]["segments"])
        self.assertEqual(self.entities()[1]["checkpoint_time"], canonical["checkpoint_time"])

    def test_watchparty_seed_waits_for_official_rounds_without_scanning_video(self):
        twitch = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,actual_start,metadata) VALUES (%s,%s,'twitch','123456789','watch_party','2026-10-04T10:00:00Z',%s)",
                           (twitch, self.broadcast, Jsonb({"vod_id": "123456789"})))
        self.store.enqueue("twitch_vod", "seed-vod", broadcast=self.broadcast, source=twitch)
        job = self.store.claim()
        media = Mock()
        capture = TwitchCapture(self.store, self.config, Processing(self.store, self.config, self.storage), media)
        with patch("pipeline.twitch.resolve_twitch", return_value={"url": "fixture", "duration": 20000}), self.assertRaises(WaitingWork):
            capture.vod(job)
        media.archive_fingerprints.assert_not_called()

    def test_watchparty_seed_failure_expands_then_preserves_full_scan_fallback(self):
        twitch = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,actual_start,metadata) VALUES (%s,%s,'twitch','123456789','watch_party','2026-10-04T10:00:00Z',%s)",
                           (twitch, self.broadcast, Jsonb({"vod_id": "123456789"})))
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1,reconciled_revision=1 WHERE id=%s", (self.broadcast,))
        self.store.execute("""INSERT INTO pipeline.match_indexes(id,broadcast_id,expected_match_id,segment_id,canonical_source_id,generation,version,archive_revision,state,rounds,provenance)
                           VALUES (%s,%s,%s,%s,%s,1,1,1,'final',%s,'{}')""",
                           (identifier(), self.broadcast, self.match, self.segment, self.source, Jsonb(series())))
        self.store.enqueue("twitch_vod", "seed-vod", broadcast=self.broadcast, source=twitch)
        job = self.store.claim()
        media = Mock()
        media.archive_fingerprints.side_effect = lambda remote, lower, upper, **kwargs: [{"time": lower, "hash": "00" * 8}]
        capture = TwitchCapture(self.store, self.config, Processing(self.store, self.config, self.storage), media)
        with patch("pipeline.twitch.resolve_twitch", return_value={"url": "fixture", "duration": 20000}):
            for attempt in range(2):
                with self.assertRaises(ContinueJob):
                    capture.vod(job)
                job = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
        self.assertEqual(job["payload"]["round_seed"]["state"], "fallback")
        self.assertEqual(job["payload"]["checkpoint"], 120)
        self.assertEqual([call.args[1:3] for call in media.archive_fingerprints.call_args_list], [(40, 220), (0, 340), (0, 120)])
        self.assertFalse(self.store.rows("SELECT * FROM pipeline.alignments WHERE source_id=%s", (twitch,)))

    def test_ambiguous_twitch_alignment_waits_only_while_capture_is_pending(self):
        twitch = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'twitch','123456789','watch_party')", (twitch, self.broadcast))
        self.store.enqueue("twitch_align", "alignment", broadcast=self.broadcast, source=twitch)
        job = self.store.claim()
        self.store.enqueue("twitch_vod", "capture", broadcast=self.broadcast, source=twitch)
        capture = TwitchCapture(self.store, self.config, Processing(self.store, self.config, self.storage))
        with self.assertRaises(WaitingWork):
            capture.align(job)
        self.store.execute("UPDATE pipeline.jobs SET state='succeeded' WHERE dedupe_key='capture'")
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1,reconciled_revision=1 WHERE id=%s", (self.broadcast,))
        with self.assertRaises(NeedsReview):
            capture.align(job)
        self.assertIsNone(self.store.one("SELECT id FROM pipeline.alignments WHERE source_id=%s", (twitch,)))


    def test_saved_intro_diagnostics_repair_segmentation_without_resetting_progress(self):
        import cv2
        import numpy as np
        from detector import Observation

        capture = self.job("live")
        source = self.entities()[1]
        detector = CandidateDetector()
        candidates = []
        for second in range(3):
            candidate = detector.observe(Observation(1000 + second, 16, 100 - second, .99))
            candidates.append(candidate)
        encoded, image = cv2.imencode(".jpg", np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertTrue(encoded)
        candidates[-1]["diagnostic_ref"] = self.storage.put("diagnostics/intro.jpg", image.tobytes())
        for value in series(base=2000):
            for second in range(3):
                candidates.append(detector.observe(Observation(value["start"] + second, value["round"], 100 - second, .99, scores=tuple(value["scores"])), extra={"teams": ["A", "B"]}))
        self.store.checkpoint(capture, source, 3300, detector.state(), candidates, [])
        self.store.finish(capture)
        media = Mock()
        media.intro_context.return_value = {"version": 1, "intro": True, "countdown_seconds": 600, "raw_lines": [["00:10:00", .99], ["A VS B", .99]]}
        processing = Processing(Store(DATABASE), self.config, self.storage, media=media)
        job = self.job("segment")
        processing.segment(job)
        processing.segment(job)
        segment, saved, _ = self.entities()
        self.assertEqual(segment["start_time"], 2000)
        self.assertEqual({value["map"] for value in segment["rounds"]}, {1})
        self.assertEqual(len(segment["rounds"]), 13)
        self.assertEqual(segment["findings"], [])
        self.assertEqual(saved["checkpoint_time"], 3300)
        self.assertEqual(saved["detector_state"], json.loads(json.dumps(detector.state())))
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.jobs WHERE kind='validate'")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.round_candidates")), len(candidates))
        media.intro_context.assert_called_once()
        media.window.assert_not_called()
        repaired = self.store.one("SELECT evidence FROM pipeline.round_candidates WHERE diagnostic_ref='diagnostics/intro.jpg'")
        self.assertTrue(repaired["evidence"]["intro_context"]["intro"])

    def test_validation_reuses_job_and_preserves_attempt_history(self):
        key = f"validate:{self.segment}:g1"
        payload = {"segment_id": str(self.segment), "segment_revision": 1}
        job = self.job("validate", key=key, payload=payload)
        self.store.finish(job, "needs_review", "Incomplete series")
        self.store.enqueue("validate", key, broadcast=self.broadcast, source=self.source, match=self.match, payload=payload)
        self.assertIsNone(self.store.claim())
        self.store.enqueue("validate", key, broadcast=self.broadcast, source=self.source, match=self.match, payload={**payload, "segment_revision": 2})
        changed = self.store.claim()
        self.assertEqual(changed["id"], job["id"])
        self.assertEqual(changed["payload"]["segment_revision"], 2)
        self.assertEqual(changed["failure_count"], 0)
        self.store.enqueue("validate", key, broadcast=self.broadcast, source=self.source, match=self.match, payload={**payload, "segment_revision": 3})
        self.assertEqual(self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]["segment_revision"], 2)
        self.store.finish(changed)
        self.store.enqueue("validate", key, broadcast=self.broadcast, source=self.source, match=self.match, payload={**payload, "segment_revision": 2})
        self.assertIsNone(self.store.claim())
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.jobs")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.attempts")), 2)

    def test_segmentation_growth_reuses_segment_and_validation_job(self):
        from detector import Observation

        self.store.execute("UPDATE pipeline.expected_matches SET best_of=3 WHERE id=%s", (self.match,))
        self.store.execute("UPDATE pipeline.segments SET start_time=200 WHERE id=%s", (self.segment,))
        processing = Processing(self.store, self.config, self.storage)
        detector = CandidateDetector()
        for count in (1, 1, 2):
            capture = self.job("live")
            source = self.entities()[1]
            candidates = []
            if count > detector.map_number or detector.previous is None:
                for value in series(maps=count)[(13 if detector.previous else 0):]:
                    for second in range(3):
                        candidate = detector.observe(Observation(value["start"] + second, value["round"], 100 - second, .99), extra={"teams": ["A", "B"]})
                        candidates.extend(detector.confirmed_candidates)
                        candidates.append(candidate)
            self.store.checkpoint(capture, source, 5000, detector.state(), candidates, [])
            self.store.finish(capture)
            segmentation = self.job("segment")
            processing.segment(segmentation)
            self.store.finish(segmentation)
            segment = self.store.one("SELECT * FROM pipeline.segments")
            self.assertEqual(segment["id"], self.segment)
            self.assertEqual(segment["start_time"], 100)
            validations = self.store.rows("SELECT * FROM pipeline.jobs WHERE kind='validate'")
            self.assertEqual(len(validations), 1)
            if count == 1:
                validation = self.store.claim()
                if validation:
                    with self.assertRaises(NeedsReview):
                        processing.validate(validation)
                    self.store.finish(validation, "needs_review", "Incomplete series")
                    findings = self.store.one("SELECT findings FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")["findings"]
                    self.assertEqual(len(findings), len({json.dumps(value, sort_keys=True) for value in findings}))
            else:
                validation = self.store.claim()
                self.assertIsNotNone(validation)
                processing.validate(validation)
                self.store.finish(validation)
                self.assertEqual({value["map"] for value in segment["rounds"]}, {1, 2})
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.segments")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.attempts a JOIN pipeline.jobs j ON j.id=a.job_id WHERE j.kind='validate'")), 2)

    def test_real_migrations_apply_idempotently(self):
        self.store.migrate()
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.migrations")), len(list((Path(__file__).resolve().parents[1] / "indexer/migrations").glob("*.sql"))))

    def test_aligner_upgrade_requeues_failed_reconciliation_without_resetting_capture(self):
        from storyboard_align import ALIGNER_VERSION

        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,))
        state = {"version": "stored", "previous": {"start": 1200}}
        self.store.execute("UPDATE pipeline.sources SET checkpoint_time=1234,detector_state=%s WHERE id=%s", (Jsonb(state), self.source))
        job = self.job("reconcile", key=f"reconcile:{self.broadcast}:1", payload={"aligner_version": "storyboard-v4"})
        self.store.finish(job, "needs_review", "Imprecise storyboard alignment")
        refresh = self.job("refresh")
        youtube = Mock()
        youtube.metadata.return_value = {"state": "archive_ready", "actual_start": self.entities()[1]["actual_start"], "actual_end": None, "metadata": {}}
        coordinator = Coordinator(self.store, self.config, youtube=youtube)
        coordinator.refresh(refresh)
        coordinator.refresh(refresh)
        self.store.finish(refresh)
        resumed = self.store.claim()
        self.assertEqual(resumed["id"], job["id"])
        self.assertEqual(resumed["payload"], {"aligner_version": ALIGNER_VERSION})
        self.assertEqual(resumed["attempt_count"], 2)
        self.assertEqual(self.entities()[1]["checkpoint_time"], 1234)
        self.assertEqual(self.entities()[1]["detector_state"], state)
        self.assertEqual(len(self.store.rows("SELECT id FROM pipeline.jobs WHERE kind='reconcile'")), 1)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.store.execute(
                "INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'youtube','abcdefghijk','canonical')",
                (identifier(), self.broadcast),
            )

    def test_manual_broadcast_assignment_is_durable_control_plane_metadata(self):
        self.store.execute(
            "UPDATE pipeline.expected_matches SET broadcast_id=NULL,completion='expected' WHERE id=%s", (self.match,)
        )
        with patch.dict(os.environ, {"DATABASE_URL": DATABASE, "SPOILLESS_PIPELINE_MODE": "shadow"}):
            with patch.object(
                sys,
                "argv",
                ["pipeline", "assign-match", str(self.match), str(self.broadcast), "--reason", "Verified matchup"],
            ):
                cli_main()
        match = self.store.one("SELECT * FROM pipeline.expected_matches WHERE id=%s", (self.match,))
        self.assertEqual(match["broadcast_id"], self.broadcast)
        self.assertEqual(match["manual_override"]["broadcast_assignment"], "Verified matchup")
        self.assertEqual(self.store.one("SELECT * FROM pipeline.jobs")["kind"], "segment")
        provider = Mock()
        provider.matches.return_value = [
            {**match, "provider": "riot", "completion": "completed", "manual_override": {}}
        ]
        ingest_schedule(self.store, [provider])
        updated = self.store.one("SELECT * FROM pipeline.expected_matches WHERE id=%s", (self.match,))
        self.assertEqual(updated["completion"], "completed")
        self.assertEqual(updated["manual_override"]["broadcast_assignment"], "Verified matchup")

    def test_schedule_identity_survives_partial_provider_listing_and_restart(self):
        self.store.execute("UPDATE pipeline.expected_matches SET provider='riot' WHERE id=%s", (self.match,))
        first = self.entities()[2]
        second = {**first, "external_id": "second-match", "team_a": "C", "team_b": "D", "match_order": 2}
        provider = Mock()
        provider.matches.return_value = [second]
        ingest_schedule(self.store, [provider])
        later = self.store.one("SELECT * FROM pipeline.expected_matches WHERE external_id='second-match'")
        self.store.execute("UPDATE pipeline.sources SET checkpoint_time=5850 WHERE id=%s", (self.source,))
        provider.matches.return_value = [{**second, "match_order": 1, "completion": "running"}]
        for _ in range(2):
            ingest_schedule(Store(DATABASE), [provider])
        unchanged = self.entities()[2]
        updated = self.store.one("SELECT * FROM pipeline.expected_matches WHERE id=%s", (later["id"],))
        self.assertEqual((unchanged["team_a"], unchanged["team_b"]), ("A", "B"))
        self.assertEqual(updated["match_order"], 2)
        self.assertEqual(updated["completion"], "running")
        self.assertEqual(updated["external_id"], "second-match")
        self.assertEqual(self.store.one("SELECT count(*) AS count FROM pipeline.expected_matches")["count"], 2)
        self.assertEqual(self.entities()[1]["checkpoint_time"], 5850)

    def test_new_provider_identity_cannot_overwrite_occupied_schedule_slot(self):
        self.store.execute("UPDATE pipeline.expected_matches SET provider='riot' WHERE id=%s", (self.match,))
        first = self.entities()[2]
        provider = Mock()
        provider.matches.return_value = [{**first, "external_id": "different-event", "team_a": "C", "team_b": "D"}]
        ingest_schedule(self.store, [provider])
        self.assertEqual(self.entities()[2], first)

    def test_verified_live_identity_keeps_teams_but_accepts_completion_by_provider_id(self):
        self.store.execute("UPDATE pipeline.expected_matches SET provider='riot' WHERE id=%s", (self.match,))
        self.store.execute("UPDATE pipeline.expected_matches SET manual_override=%s WHERE id=%s", (Jsonb({"live_identity_repair": "Verified stored gameplay HUD"}), self.match))
        first = self.entities()[2]
        provider = Mock()
        provider.matches.return_value = [{**first, "completion": "completed", "team_a": "wrong", "team_b": "wrong", "manual_override": {}}]
        ingest_schedule(self.store, [provider])
        updated = self.entities()[2]
        self.assertEqual(updated["completion"], "completed")
        self.assertEqual((updated["team_a"], updated["team_b"]), ("A", "B"))
        self.assertEqual(updated["manual_override"], first["manual_override"])

    def test_excluded_vertical_broadcast_is_not_refreshed(self):
        self.store.execute("UPDATE pipeline.broadcasts SET state='unavailable',metadata=%s WHERE id=%s", (Jsonb({"manual_exclusion": "Vertical companion"}), self.broadcast))
        youtube = Mock()
        Coordinator(self.store, self.config, youtube=youtube).refresh({"broadcast_id": self.broadcast})
        youtube.metadata.assert_not_called()
        self.assertEqual(self.store.one("SELECT state FROM pipeline.broadcasts WHERE id=%s", (self.broadcast,))["state"], "unavailable")

    def test_concurrent_claims_are_unique(self):
        for number in range(8):
            self.store.enqueue("validate", f"claim:{number}")
        with ThreadPoolExecutor(max_workers=8) as executor:
            jobs = list(executor.map(lambda _: self.store.claim(), range(8)))
        self.assertEqual(len({job["id"] for job in jobs}), 8)
        self.assertTrue(all(job["state"] == "running" for job in jobs))

    def test_concurrent_claims_serialize_the_same_broadcast(self):
        for number in range(8):
            self.store.enqueue("validate", f"broadcast-claim:{number}", broadcast=self.broadcast)
        with ThreadPoolExecutor(max_workers=8) as executor:
            claimed = list(executor.map(lambda _: self.store.claim(), range(8)))
        jobs = [job for job in claimed if job]
        self.assertEqual(len(jobs), 1)
        self.store.finish(jobs[0])
        self.assertIsNotNone(self.store.claim())

    def test_concurrent_claims_serialize_sources_without_broadcast_assignment(self):
        for number in range(4):
            self.store.enqueue("twitch_live", f"source-claim:{number}", source=self.source)
        with ThreadPoolExecutor(max_workers=4) as executor:
            jobs = list(executor.map(lambda _: self.store.claim(), range(4)))
        self.assertEqual(sum(job is not None for job in jobs), 1)

    def test_two_worker_slots_process_different_broadcasts_concurrently_and_stop(self):
        second_broadcast = identifier()
        self.store.execute(
            """INSERT INTO pipeline.broadcasts(id,event,day,region,channel_id,youtube_id,state)
               VALUES (%s,'Champions','2026-10-04','international','other','lmnopqrstuv','archive_ready')""",
            (second_broadcast,),
        )
        for broadcast in [self.broadcast, second_broadcast]:
            self.store.enqueue("probe_archive", f"parallel:{broadcast}", broadcast=broadcast)
        entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
        jobs = []

        def analyze(job):
            with lock:
                jobs.append(job)
                if len(jobs) == 2:
                    entered.set()
            if not release.wait(10):
                raise TimeoutError("The second processing slot did not start")

        first, second = Mock(), Mock()
        first.probe_archive.side_effect = analyze
        second.probe_archive.side_effect = analyze
        worker = Worker(self.store, self.config, self.storage, coordinator=Mock(), processing=first)
        completed = []
        finish = self.store.finish

        def finish_and_stop(job, *args, **kwargs):
            result = finish(job, *args, **kwargs)
            with lock:
                completed.append(job)
                if len(completed) == 2:
                    worker.stop.set()
            return result

        with patch("pipeline.worker.Processing", return_value=second) as factory:
            with patch.object(self.store, "finish", side_effect=finish_and_stop):
                thread = threading.Thread(target=worker.run, daemon=True)
                thread.start()
                try:
                    self.assertTrue(entered.wait(10))
                    self.assertEqual(len({job["broadcast_id"] for job in jobs}), 2)
                    self.assertEqual(self.store.one("SELECT count(*) AS count FROM pipeline.jobs WHERE state='running'")["count"], 2)
                    factory.assert_called_once()
                    self.assertIs(second.stop, worker.stop)
                finally:
                    release.set()
                    thread.join(timeout=10)
                    worker.stop.set()
                self.assertFalse(thread.is_alive())
        self.assertEqual(self.store.one("SELECT count(*) AS count FROM pipeline.jobs WHERE state='succeeded'")["count"], 2)

    def test_discovery_failure_backoff_preserves_existing_queued_archive_work(self):
        channel = {"channel_id": "official", "includeTitle": "Champions", "excludeTitle": "HIGHLIGHTS"}
        config = Config(DATABASE, settings={"youtube_channels": [channel], "full_match_channels": [channel]})
        youtube = Mock()
        youtube.discover.side_effect = WaitingSource("RSS and channel tab temporarily unavailable")
        coordinator = Coordinator(self.store, config, youtube)
        coordinator.discover()
        coordinator.discover()
        discovery = self.store.one("SELECT * FROM pipeline.jobs WHERE kind='discover_youtube'")
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.jobs WHERE kind='discover_youtube'")), 1)
        self.store.execute("UPDATE pipeline.jobs SET priority=100 WHERE id=%s", (discovery["id"],))
        processing = Mock()
        worker = Worker(self.store, config, self.storage, coordinator=coordinator, processing=processing)
        with self.assertLogs("pipeline.worker", level="WARNING") as logs:
            worker.run_once()
        failed = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (discovery["id"],))
        self.assertEqual(failed["state"], "waiting_source")
        self.assertEqual(failed["failure_count"], 1)
        self.assertEqual(failed["attempt_count"], 1)
        self.assertNotIn("Traceback", "\n".join(logs.output))
        coordinator.discover()
        self.assertEqual(self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (discovery["id"],))["available_at"], failed["available_at"])
        self.store.enqueue("recover", "existing-archive", broadcast=self.broadcast, source=self.source, payload={"checkpoint": 7740}, priority=10)
        worker.run_once()
        processing.recover.assert_called_once()
        self.assertEqual(processing.recover.call_args.args[0]["payload"]["checkpoint"], 7740)
        self.assertEqual(self.store.one("SELECT * FROM pipeline.jobs WHERE dedupe_key='existing-archive'")["payload"]["checkpoint"], 7740)

    def test_discovery_is_idempotent_for_existing_broadcasts_and_uploads(self):
        channel = {"channel_id": "official", "includeTitle": "Champions", "excludeTitle": "HIGHLIGHTS"}
        youtube = Mock()
        youtube.discover.return_value = [{"id": "abcdefghijk", "title": "Champions"}] * 2
        coordinator = Coordinator(self.store, self.config, youtube)
        routes = [{"kind": "associate", "channel": channel}, {"kind": "associate_upload", "channel": channel}]
        job = self.job("discover_youtube", payload={"routes": routes})
        coordinator.discover_youtube(job)
        coordinator.discover_youtube(job)
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.jobs WHERE kind='associate_upload'")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.jobs WHERE kind='associate'")), 0)
        upload = identifier()
        self.store.execute(
            "INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'youtube','lmnopqrstuv','full_match')",
            (upload, self.broadcast),
        )
        youtube.discover.return_value = [{"id": "lmnopqrstuv", "title": "Champions"}]
        coordinator.discover_youtube({"payload": {"routes": [routes[1]]}})
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.jobs WHERE kind='associate_upload'")), 1)
        self.assertEqual(youtube.discover.call_args.kwargs, {"uploads": True})

    def test_discovery_route_failure_preserves_other_route_success(self):
        channel = {"channel_id": "official"}
        youtube = Mock()
        youtube.discover.side_effect = [WaitingSource("Streams tab unavailable"), [{"id": "lmnopqrstuv", "title": "FULL MATCH"}]]
        coordinator = Coordinator(self.store, self.config, youtube)
        routes = [{"kind": "associate", "channel": channel}, {"kind": "associate_upload", "channel": channel}]
        with self.assertRaises(WaitingSource):
            coordinator.discover_youtube({"payload": {"routes": routes}})
        self.assertIsNotNone(self.store.one("SELECT * FROM pipeline.jobs WHERE dedupe_key='associate-upload:lmnopqrstuv'"))

    def test_upload_without_segment_cannot_starve_archive_readiness(self):
        self.store.execute("DELETE FROM pipeline.segments")
        self.store.enqueue(
            "validate_upload",
            "upload-before-segment",
            broadcast=self.broadcast,
            source=self.source,
            match=self.match,
            priority=100,
        )
        self.store.enqueue("probe_archive", "official-first", broadcast=self.broadcast, source=self.source)
        claimed = self.store.claim()
        self.assertEqual(claimed["kind"], "probe_archive")
        self.assertIsNone(self.store.claim())
        upload = self.store.one("SELECT * FROM pipeline.jobs WHERE kind='validate_upload'")
        self.assertEqual(upload["attempt_count"], 0)
        self.assertEqual(upload["failure_count"], 0)

    def test_completed_upload_rechecks_new_segment_without_repeating_ocr(self):
        self.store.execute("UPDATE pipeline.expected_matches SET best_of=3 WHERE id=%s", (self.match,))
        upload = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,checkpoint_time,metadata) VALUES (%s,%s,'youtube','fullupload','full_match',4302,%s)", (upload, self.broadcast, Jsonb({"expected_match_id": str(self.match)})))
        self.store.enqueue("recover", f"archive-backfill:{self.source}", broadcast=self.broadcast, source=self.source, payload={"checkpoint": 2000}, delay=60)
        self.store.enqueue("validate_upload", f"validate-upload:{upload}", broadcast=self.broadcast, source=upload, match=self.match, payload={"segment_revision": 1})
        job = self.store.claim()
        source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (upload,))
        candidates = [{"accepted": True, "confidence": .99, "detector_version": "fixture", "media_time": value["start"], "round_number": value["round"], "scores": value["scores"], "evidence": {"start": value["start"], "broadcast_map": value["map"]}} for value in series(maps=2)]
        self.store.checkpoint(job, source, 4302, {}, candidates, [], timeline="archive")
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"duration": 4303, "url": "fixture"}
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        with self.assertRaisesRegex(WaitingSource, "canonical archive coverage"):
            processing.validate_upload(job)
        self.store.finish(job, "waiting_source", "Canonical archive coverage")
        self.store.execute("UPDATE pipeline.segments SET rounds=%s,revision=2 WHERE id=%s", (Jsonb(series(maps=2)), self.segment))
        self.store.enqueue("validate_upload", f"validate-upload:{upload}", broadcast=self.broadcast, source=upload, match=self.match, payload={"segment_revision": 2})
        resumed = self.store.claim()
        self.assertEqual(resumed["id"], job["id"])
        processing.validate_upload(resumed)
        self.store.finish(resumed)
        media.window.assert_not_called()
        self.assertEqual(self.store.one("SELECT checkpoint_time FROM pipeline.sources WHERE id=%s", (upload,))["checkpoint_time"], 4302)
        self.store.execute("UPDATE pipeline.jobs SET state='succeeded' WHERE dedupe_key=%s", (f"archive-backfill:{self.source}",))
        self.store.execute("UPDATE pipeline.segments SET rounds=%s,revision=3 WHERE id=%s", (Jsonb(series()), self.segment))
        self.store.enqueue("validate_upload", f"validate-upload:{upload}", broadcast=self.broadcast, source=upload, match=self.match, payload={"segment_revision": 3})
        genuine = self.store.claim()
        with self.assertRaisesRegex(NeedsReview, "sequence disagrees"):
            processing.validate_upload(genuine)
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.jobs WHERE kind='validate_upload'")), 1)

    def test_secondary_recovery_filters_unrelated_matches(self):
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,))
        matching = identifier()
        for source_id, match_id in [(matching, self.match), (identifier(), identifier())]:
            self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,metadata) VALUES (%s,%s,'youtube',%s,'full_match',%s)", (source_id, self.broadcast, str(source_id), Jsonb({"expected_match_id": str(match_id)})))
        job = self.job("recover", payload={"start": 100, "end": 200, "timeline": "archive", "stage": 3})
        processing = Processing(self.store, self.config, self.storage)
        processing.recover_secondary = Mock()
        processing.recover(job)
        self.assertEqual(processing.recover_secondary.call_count, 1)
        self.assertEqual(processing.recover_secondary.call_args.args[3]["id"], matching)

    def test_fallback_access_failure_remains_retryable_and_preserves_checkpoint(self):
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,))
        upload = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,metadata) VALUES (%s,%s,'youtube','fullupload','full_match',%s)", (upload, self.broadcast, Jsonb({"expected_match_id": str(self.match)})))
        payload = {"start": 100, "end": 200, "checkpoint": 150, "timeline": "archive", "stage": 3}
        self.store.enqueue("recover", "fallback-access", broadcast=self.broadcast, source=self.source, match=self.match, payload=payload)
        processing = Processing(self.store, self.config, self.storage)
        processing.recover_secondary = Mock(side_effect=WaitingSource("Server returned 403 Forbidden"))
        Worker(self.store, self.config, self.storage, processing=processing).run_once()
        saved = self.store.one("SELECT * FROM pipeline.jobs WHERE dedupe_key='fallback-access'")
        self.assertEqual(saved["state"], "waiting_source")
        self.assertEqual(saved["failure_count"], 1)
        self.assertEqual(saved["payload"], payload)

    def test_completed_recovery_is_not_reopened_or_loses_checkpoint(self):
        payload = {"start": 100, "end": 200, "stage": 2}
        job = self.job("recover", key="completed-range", payload=payload)
        saved_payload = {**payload, "checkpoint": 200, "detector_state": {"version": "fixture"}}
        self.store.execute("UPDATE pipeline.jobs SET payload=%s WHERE id=%s", (Jsonb(saved_payload), job["id"]))
        self.store.finish(job)
        for _ in range(3):
            self.store.enqueue("recover", "completed-range", broadcast=self.broadcast, source=self.source, match=self.match, payload=payload)
        saved = self.store.one("SELECT * FROM pipeline.jobs WHERE dedupe_key='completed-range'")
        self.assertEqual(saved["state"], "succeeded")
        self.assertEqual(saved["payload"], saved_payload)
        self.assertIsNone(self.store.claim())
        self.store.execute("UPDATE pipeline.broadcasts SET generation=generation+1 WHERE id=%s", (self.broadcast,))
        self.store.enqueue("recover", "completed-range", broadcast=self.broadcast, source=self.source, match=self.match, payload=payload)
        self.assertEqual(self.store.claim()["generation"], 2)

    def test_terminal_recovery_extension_keeps_checkpoint_and_running_or_review_state(self):
        payload = {"start": 100, "end": 500, "stage": 2, "timeline": "archive", "terminal_map": 1}
        job = self.job("recover", key="stable-tail", payload=payload)
        progress = {**payload, "checkpoint": 280, "detector_state": {"version": DETECTOR_VERSION}, "recovered": 2}
        self.store.execute("UPDATE pipeline.jobs SET payload=%s WHERE id=%s", (Jsonb(progress), job["id"]))
        self.store.finish(job, "queued")
        self.store.enqueue("recover", "stable-tail", broadcast=self.broadcast, source=self.source, match=self.match,
                           payload={**payload, "start": 200})
        self.assertEqual(self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"], progress)
        self.store.enqueue("recover", "stable-tail", broadcast=self.broadcast, source=self.source, match=self.match,
                           payload={**payload, "start": 200, "end": 800})
        saved = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
        self.assertEqual(saved["payload"], {**progress, "end": 800})
        active = self.store.claim(job_id=job["id"])
        self.store.enqueue("recover", "stable-tail", broadcast=self.broadcast, source=self.source, match=self.match,
                           payload={**payload, "end": 1000})
        saved = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
        self.assertEqual(saved["state"], "running")
        self.assertEqual(saved["lease_token"], active["lease_token"])
        self.assertEqual(saved["payload"]["end"], 800)
        self.store.finish(active)
        self.store.enqueue("recover", "stable-tail", broadcast=self.broadcast, source=self.source, match=self.match,
                           payload={**payload, "end": 1000})
        self.assertEqual(self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"], {**progress, "end": 1000})
        active = self.store.claim(job_id=job["id"])
        self.store.finish(active, "needs_review", "Evidence needs review")
        self.store.enqueue("recover", "stable-tail", broadcast=self.broadcast, source=self.source, match=self.match,
                           payload={**payload, "end": 1200})
        self.assertEqual(self.store.one("SELECT state FROM pipeline.jobs WHERE id=%s", (job["id"],))["state"], "needs_review")
        self.store.execute("UPDATE pipeline.broadcasts SET generation=2 WHERE id=%s", (self.broadcast,))
        self.store.enqueue("recover", "stable-tail", broadcast=self.broadcast, source=self.source, match=self.match, payload=payload)
        saved = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
        self.assertEqual(saved["generation"], 2)
        self.assertEqual(saved["payload"], payload)

    def test_overlapping_recovery_replays_complete_compatible_ocr_without_media(self):
        from detector import Observation, DETECTOR_VERSION as OCR_VERSION
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,))
        seed = self.job("recover", key="saved-ocr")
        source = self.entities()[1]
        processing = Processing(self.store, self.config, self.storage)
        processing.save_alignment(seed, source, source, {"timelineScale": 1, "segments": [{"sourceStart": 0, "sourceEnd": 100, "offset": 0}], "anchors": 0, "maximumResidual": 0}, "reconciliation", 1)
        detector = CandidateDetector()
        extra = {"capture": {"ocr_version": OCR_VERSION, "archive_revision": 1, "mode": "dense",
                              "aliases_signature": hashlib.sha256(json.dumps(processing.context(seed)[3], sort_keys=True).encode()).hexdigest()},
                 "provenance": {"method": "direct_official_archive", "revision": 1}}
        rows = [detector.observe(Observation(second, 8, 100 - second, .99, scores=(7, 0)), extra=extra) for second in range(9)]
        self.store.checkpoint(seed, source, -1, source["detector_state"], rows, [], timeline="archive")
        self.store.finish(seed)
        job = self.job("recover", key="overlap", payload={"start": 0, "end": 9, "timeline": "archive"})
        youtube, media = Mock(), Mock()
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        processing.recover(job)
        youtube.resolve.assert_not_called()
        media.window.assert_not_called()
        saved = self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]
        self.assertEqual(saved["checkpoint"], 9)
        self.assertEqual(saved["detector_state"]["last_time"], 8)
        self.assertEqual(saved["recovered"], 1)
        self.assertEqual(self.entities()[1]["checkpoint_time"], -1)
        self.assertEqual(self.store.one("SELECT count(*) AS count FROM pipeline.round_candidates WHERE accepted")["count"], 1)
        self.assertEqual(self.store.one("SELECT evidence->'capture'->>'mode' AS mode FROM pipeline.round_candidates WHERE NOT accepted LIMIT 1")["mode"], "dense")

        self.store.finish(job)
        self.store.execute("UPDATE pipeline.jobs SET state='succeeded' WHERE state='queued'")
        seed = self.job("recover", key="legacy-blank-seed")
        blanks = [detector.observe(Observation(second, None, None, 0), extra={"provenance": extra["provenance"]}) for second in [9, 10]]
        self.store.checkpoint(seed, source, -1, source["detector_state"], blanks, [], timeline="archive")
        self.store.finish(seed)
        accepted = self.store.one("SELECT * FROM pipeline.round_candidates WHERE accepted")
        for number in range(2):
            self.store.execute("UPDATE pipeline.jobs SET state='succeeded' WHERE state='queued'")
            job = self.job("recover", key=f"archive-tail:legacy-blank-{number}", payload={"start": 0, "end": 11, "timeline": "archive"})
            youtube.reset_mock()
            media.reset_mock()
            youtube.resolve.return_value = {"url": "fixture", "duration": 11}
            media.window.side_effect = lambda remote, start, end, *args, **kwargs: (
                (Observation(second, None, None, 0), {}, None) for second in range(int(start), int(end)))
            processing.recover(job)
            if number == 0:
                self.assertEqual(media.window.call_args.args[1:3], (9, 11))
            else:
                youtube.resolve.assert_not_called()
                media.window.assert_not_called()
            self.assertEqual(self.store.one("SELECT * FROM pipeline.round_candidates WHERE accepted"), accepted)
            self.store.finish(job)

    def test_cached_ocr_rescans_missing_unreliable_and_incompatible_samples(self):
        from detector import Observation, DETECTOR_VERSION as OCR_VERSION
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,))
        seed = self.job("recover", key="cache-seed")
        source = self.entities()[1]
        processing = Processing(self.store, self.config, self.storage)
        processing.save_alignment(seed, source, source, {"timelineScale": 1, "segments": [{"sourceStart": 0, "sourceEnd": 100, "offset": 0}], "anchors": 0, "maximumResidual": 0}, "reconciliation", 1)
        detector = CandidateDetector()
        capture = {"ocr_version": OCR_VERSION, "archive_revision": 1, "mode": "dense",
                   "aliases_signature": hashlib.sha256(json.dumps(processing.context(seed)[3], sort_keys=True).encode()).hexdigest()}
        rows = [detector.observe(Observation(second, 8, 100 - second, .99, scores=(7, 0)),
                                 extra={"capture": capture, "provenance": {"method": "direct_official_archive", "revision": 1}}) for second in range(9)]
        self.store.checkpoint(seed, source, -1, source["detector_state"], rows, [], timeline="archive")
        self.store.finish(seed)
        for variant in ["old_ocr", "old_archive", "adaptive", "aliases", "unreliable", "hole"]:
            with self.subTest(variant=variant):
                self.store.execute("UPDATE pipeline.round_candidates SET confidence=.99,evidence=jsonb_set(evidence,'{capture}',%s)", (Jsonb(capture),))
                changed = {**capture}
                if variant == "old_ocr":
                    changed["ocr_version"] = "old"
                elif variant == "old_archive":
                    changed["archive_revision"] = 0
                elif variant == "adaptive":
                    changed["mode"] = "adaptive"
                elif variant == "aliases":
                    changed["aliases_signature"] = "other aliases"
                if changed != capture:
                    self.store.execute("UPDATE pipeline.round_candidates SET evidence=jsonb_set(evidence,'{capture}',%s)", (Jsonb(changed),))
                if variant == "unreliable":
                    self.store.execute("UPDATE pipeline.round_candidates SET confidence=.5 WHERE media_time=4")
                if variant == "hole":
                    self.store.execute("DELETE FROM pipeline.round_candidates WHERE media_time=4")
                self.store.execute("UPDATE pipeline.jobs SET state='succeeded' WHERE state='queued'")
                job = self.job("recover", key=variant, payload={"start": 0, "end": 9, "timeline": "archive"})
                youtube, media = Mock(), Mock()
                youtube.resolve.return_value = {"url": "fixture", "duration": 9}
                media.window.side_effect = lambda remote, start, end, *args, **kwargs: (
                    (Observation(second, 8, 100 - second, .99, scores=(7, 0)), {}, None) for second in range(int(start), int(end)))
                processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
                processing.save_diagnostic = Mock()
                processing.recover(job)
                self.assertEqual(media.window.call_args.args[1:3], (4, 5) if variant in {"unreliable", "hole"} else (0, 9))
                self.assertEqual(self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]["checkpoint"], 9)
                self.store.finish(job)

    def test_upload_internal_gap_waits_for_recovery_then_revalidates_without_ocr(self):
        upload = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,checkpoint_time) VALUES (%s,%s,'youtube','fullupload','full_match',1300)", (upload, self.broadcast))
        rounds = series()
        missing = [value for value in rounds if value["round"] != 5]
        self.store.execute("UPDATE pipeline.segments SET rounds=%s,evidence=%s WHERE id=%s", (Jsonb(missing), Jsonb({"closed": True}), self.segment))
        self.store.enqueue("validate_upload", "upload-gap-check", broadcast=self.broadcast, source=upload, match=self.match)
        job = self.store.claim()
        source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (upload,))
        candidates = [{"accepted": True, "confidence": .99, "detector_version": "fixture", "media_time": value["start"], "round_number": value["round"], "scores": value["scores"], "evidence": {"start": value["start"], "broadcast_map": value["map"]}} for value in rounds]
        self.store.checkpoint(job, source, 1300, {}, candidates, [], timeline="archive")
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"duration": 1301, "url": "fixture"}
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        with self.assertRaisesRegex(WaitingWork, "targeted canonical recovery"):
            processing.validate_upload(job)
        self.store.finish(job, "waiting_source", dependency=True)
        recovery = self.store.claim()
        self.assertEqual(recovery["kind"], "recover")
        self.store.finish(recovery)
        resumed = self.store.claim()
        with self.assertRaisesRegex(NeedsReview, "sequence disagrees"):
            processing.validate_upload(resumed)
        self.assertEqual(self.store.one("SELECT state FROM pipeline.jobs WHERE kind='recover'")["state"], "succeeded")
        self.store.execute("UPDATE pipeline.segments SET rounds=%s,revision=revision+1 WHERE id=%s", (Jsonb(rounds), self.segment))
        processing.validate_upload(resumed)
        media.window.assert_not_called()
        self.assertEqual(self.store.one("SELECT checkpoint_time FROM pipeline.sources WHERE id=%s", (upload,))["checkpoint_time"], 1300)

    def test_validation_outcomes_always_refresh_export(self):
        for outcome in [None, WaitingWork("Recovery active"), NeedsReview("Contradiction")]:
            with self.subTest(outcome=outcome):
                self.store.execute("DELETE FROM pipeline.attempts")
                self.store.execute("DELETE FROM pipeline.jobs")
                self.store.enqueue("validate_upload", "upload-check", broadcast=self.broadcast, source=self.source, match=self.match)
                processing = Mock()
                processing.validate_upload.side_effect = outcome
                Worker(self.store, self.config, self.storage, processing=processing).run_once()
                self.assertEqual(self.store.one("SELECT state FROM pipeline.jobs WHERE kind='export'")["state"], "queued")
                if isinstance(outcome, WaitingWork):
                    self.assertEqual(self.store.one("SELECT failure_count FROM pipeline.jobs WHERE kind='validate_upload'")["failure_count"], 0)

    def test_aligned_recovery_resumes_its_stored_window(self):
        from detector import Observation

        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,))
        upload = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'youtube','fullupload','full_match')", (upload, self.broadcast))
        job = self.job("recover", payload={"start": 1000, "end": 1300, "timeline": "archive", "stage": 3})
        source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (upload,))
        canonical = self.entities()[1]
        self.store.checkpoint(job, canonical, -1, {}, [{"accepted": True, "confidence": .99, "detector_version": "fixture", "media_time": 900, "round_number": 1, "evidence": {"start": 900, "broadcast_map": 1}}], [], timeline="archive")
        self.store.checkpoint(job, source, -1, {}, [], fingerprints()["frames"], timeline="archive")
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"url": "fixture"}
        media.window.side_effect = lambda remote, start, end, aliases, **kwargs: [(Observation(start + second, 2 + int(start / 90), 100 - second, .99), {}, None) for second in range(3)]
        mapping = {"timelineScale": 1, "segments": [{"sourceStart": 0, "sourceEnd": 300, "canonicalStart": 1000, "canonicalEnd": 1300, "offset": 1000}], "maximumResidual": 0, "anchors": 30}
        with patch("pipeline.processing.piecewise_alignment", return_value=mapping):
            for position in [0, 90]:
                processing = Processing(Store(DATABASE), self.config, self.storage, youtube=youtube, media=media)
                with self.assertRaises(ContinueJob):
                    processing.recover_secondary(job, self.store.one("SELECT * FROM pipeline.broadcasts"), canonical, source, 1000, 1300, {})
                saved = self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]
                self.assertEqual(saved["secondary_progress"]["sections"]["0"]["checkpoint"], position + 90)
                self.assertEqual(media.window.call_args.args[1:3], (position, position + 90))
                self.store.finish(job, "queued")
                job = self.store.claim()
                while job["kind"] == "segment":
                    self.store.finish(job)
                    job = self.store.claim()
        self.assertEqual(self.entities()[1]["checkpoint_time"], -1)

    def test_upload_checks_segment_before_any_media_processing(self):
        self.store.execute("UPDATE pipeline.sources SET role='full_match' WHERE id=%s", (self.source,))
        job = self.job("validate_upload")
        self.store.execute("DELETE FROM pipeline.segments")
        youtube, media = Mock(), Mock()
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        with self.assertRaises(WaitingSource):
            processing.validate_upload(job)
        youtube.resolve.assert_not_called()
        media.window.assert_not_called()

    def test_upload_yields_and_resumes_without_blocking_canonical_jobs(self):
        from detector import Observation
        import numpy as np

        upload = identifier()
        self.store.execute(
            "INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'youtube','uploadvideo','full_match')",
            (upload, self.broadcast),
        )
        self.store.enqueue(
            "validate_upload",
            "upload-windows",
            broadcast=self.broadcast,
            source=upload,
            match=self.match,
        )
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"url": "fixture", "duration": 1250}
        frame = np.zeros((324, 1280, 3), dtype=np.uint8)
        media.window.side_effect = lambda remote, start, end, *args, **kwargs: (
            (
                Observation(item["start"] + second, item["round"], 100 - second, 0.99, scores=tuple(item["scores"])),
                {"teams": ["A", "B"]},
                frame,
            )
            for item in series(base=0)
            for second in range(3)
            if start <= item["start"] + second < end
        )
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        worker = Worker(self.store, self.config, self.storage, coordinator=Mock(), processing=processing)
        worker.run_once()
        job = self.store.one("SELECT * FROM pipeline.jobs WHERE dedupe_key='upload-windows'")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["failure_count"], 0)
        self.assertEqual(self.store.one("SELECT * FROM pipeline.attempts")["state"], "yielded")
        self.assertEqual(
            self.store.one("SELECT checkpoint_time FROM pipeline.sources WHERE id=%s", (upload,))["checkpoint_time"], 89
        )
        canonical = Mock()
        worker.handlers["probe_archive"] = canonical
        self.store.enqueue(
            "probe_archive", "canonical-between-windows", broadcast=self.broadcast, source=self.source, priority=10
        )
        worker.run_once()
        canonical.assert_called_once()
        resumed = Worker(Store(DATABASE), self.config, self.storage, coordinator=Mock(), processing=processing)
        for _ in range(13):
            self.store.execute("UPDATE pipeline.jobs SET available_at=now() WHERE id=%s", (job["id"],))
            resumed.run_once()
        self.assertEqual(
            self.store.one("SELECT state FROM pipeline.jobs WHERE id=%s", (job["id"],))["state"], "succeeded"
        )
        self.assertEqual(media.window.call_count, 14)
        self.assertTrue(all(call.args[2] - call.args[1] <= 90 for call in media.window.call_args_list))
        self.assertEqual(
            self.store.one(
                "SELECT count(*) AS count FROM pipeline.round_candidates WHERE source_id=%s AND accepted", (upload,)
            )["count"],
            13,
        )
        self.assertEqual(self.entities()[0]["rounds"], series())

    def test_reconciliation_yields_after_one_fingerprint_window(self):
        self.store.execute(
            "UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,)
        )
        job = self.job("reconcile")
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"url": "fixture", "duration": 300}
        frames = fingerprints(count=150, interval=2)["frames"]
        _, source, _ = self.entities()
        self.store.checkpoint(job, source, 298, {}, [], frames)
        media.archive_fingerprints.side_effect = lambda remote, start, end, **kwargs: [
            frame for frame in frames if start <= frame["time"] < end
        ]
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        for checkpoint in [118, 238]:
            with self.assertRaises(ContinueJob):
                processing.reconcile(job)
            self.assertEqual(self.store.status(self.broadcast)["sources"][0]["archive_fingerprint_time"], checkpoint)
            self.store.finish(job, "queued")
            job = self.store.claim()
        processing.reconcile(job)
        self.assertEqual(
            [(call.args[1], call.args[2]) for call in media.archive_fingerprints.call_args_list],
            [(0, 120), (120, 240), (240, 300)],
        )
        self.assertEqual(self.store.one("SELECT * FROM pipeline.broadcasts")["reconciled_revision"], 1)
        media.window.assert_not_called()

    def test_late_start_backfill_begins_without_full_archive_fingerprinting(self):
        self.store.execute(
            "UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,)
        )
        job = self.job("reconcile")
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"url": "fixture", "duration": 18000}
        Processing(self.store, self.config, self.storage, youtube=youtube, media=media).reconcile(job)
        media.archive_fingerprints.assert_not_called()
        recovery = self.store.one("SELECT * FROM pipeline.jobs WHERE kind='recover'")
        self.assertEqual(recovery["payload"], {"start": 0, "end": 18000, "stage": 2, "timeline": "archive"})
        self.assertEqual(self.store.one("SELECT * FROM pipeline.broadcasts")["reconciled_revision"], 1)
        self.assertIsNotNone(self.store.one("SELECT * FROM pipeline.jobs WHERE kind='fingerprint_archive'"))

    def test_reconciliation_schedules_only_uncaptured_official_tail(self):
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,))
        job = self.job('reconcile')
        _, source, _ = self.entities()
        frames = fingerprints(count=90, interval=2)['frames']
        self.store.checkpoint(job, source, 178, {}, [], frames)
        youtube = Mock()
        youtube.resolve.return_value = {'url': 'fixture', 'duration': 600}
        mapping = {'segments': [{'sourceStart': 0, 'sourceEnd': 178, 'canonicalStart': 10, 'canonicalEnd': 188, 'offset': 10}],
                   'timelineScale': 1, 'anchors': 90, 'maximumResidual': 0}
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=Mock())
        with patch.object(processing, 'fingerprint_archive'), patch('pipeline.processing.piecewise_alignment', return_value=mapping):
            processing.reconcile(job)
            processing.reconcile(job)
        tail = self.store.rows("SELECT * FROM pipeline.jobs WHERE dedupe_key LIKE 'archive-tail:%%'")
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]['payload'], {'start': 158, 'end': 600, 'stage': 2, 'timeline': 'archive'})
        self.assertEqual(self.store.one('SELECT checkpoint_time FROM pipeline.sources WHERE id=%s', (self.source,))['checkpoint_time'], 178)

    def test_verified_live_coverage_reconciles_before_full_archive_fingerprinting(self):
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,))
        job = self.job('reconcile')
        _, source, _ = self.entities()
        frames = fingerprints(count=750, interval=2)['frames']
        self.store.checkpoint(job, source, 1490, {}, [], frames[::5])
        self.store.checkpoint(job, source, 1490, {}, [], frames, revision=1, timeline='archive')
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {'url': 'fixture', 'duration': 1800}
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        processing.reconcile(job)
        media.archive_fingerprints.assert_not_called()
        self.assertEqual(self.store.one('SELECT reconciled_revision FROM pipeline.broadcasts WHERE id=%s', (self.broadcast,))['reconciled_revision'], 1)
        self.assertIsNotNone(self.store.one("SELECT id FROM pipeline.jobs WHERE kind='fingerprint_archive'"))
        self.assertIsNotNone(self.store.one("SELECT id FROM pipeline.jobs WHERE dedupe_key LIKE 'archive-tail:%%'"))

    def test_archive_ocr_persists_fingerprints_without_second_decode_or_checkpoint_reset(self):
        from detector import Observation
        import numpy as np

        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=2 WHERE id=%s", (self.broadcast,))
        job = self.job("recover", key=f"archive-backfill:{self.source}", payload={"start": 0, "end": 90, "timeline": "archive"})
        source = self.entities()[1]
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"url": "fixture", "duration": 90}
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        media.window.side_effect = lambda remote, start, end, *args, **kwargs: [(Observation(time, 1, 100 - time, .99, scores=(0, 0)), {}, frame) for time in range(int(start), int(end))]
        media.fingerprint.return_value = {"hash": "ff" * 8, "gameplayHash": "aa" * 8}
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        processing.save_alignment(job, source, source, {"timelineScale": 1, "segments": [{"sourceStart": 0, "sourceEnd": 90, "offset": 0}], "anchors": 0, "maximumResidual": 0}, "reconciliation", 2)
        processing.recover(job)
        saved = self.store.fingerprints(source, "archive", 2)
        self.assertEqual([value["time"] for value in saved["frames"]], list(range(0, 90, 2)))
        self.assertEqual(self.store.fingerprints(source, "archive", 1)["frames"], [])
        self.assertEqual(media.fingerprint.call_count, 45)
        processing.fingerprint_archive(job)
        media.archive_fingerprints.assert_not_called()
        restart = Processing(Store(DATABASE), self.config, self.storage, youtube=youtube, media=media)
        restart.recover(job)
        self.assertEqual(media.window.call_count, 1)
        self.assertEqual(self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]["checkpoint"], 90)
        self.assertEqual(self.entities()[1]["checkpoint_time"], -1)

    def test_fingerprint_resume_fills_interior_gap_without_redecoding_covered_ranges(self):
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,))
        job = self.job("fingerprint_archive")
        source = self.entities()[1]
        frames = fingerprints(count=6, interval=2)["frames"]
        self.store.checkpoint(job, source, -1, {}, [], [frame for frame in frames if frame["time"] not in {4, 6}], revision=1, timeline="archive")
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"url": "fixture", "duration": 12}
        media.archive_fingerprints.side_effect = lambda remote, start, end, **kwargs: [frame for frame in frames if start <= frame["time"] < end]
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        processing.fingerprint_archive(job)
        self.assertEqual(media.archive_fingerprints.call_args.args[1:3], (4, 8))
        restart = Processing(Store(DATABASE), self.config, self.storage, youtube=youtube, media=media)
        restart.fingerprint_archive(job)
        self.assertEqual(media.archive_fingerprints.call_count, 1)
        self.assertEqual(self.store.fingerprints(source, "archive", 1)["frames"], frames)
        self.assertEqual(self.entities()[1]["checkpoint_time"], -1)

    def test_fingerprint_future_anchor_does_not_hide_missing_prefix_and_yields(self):
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,))
        job = self.job("fingerprint_archive")
        source = self.entities()[1]
        frames = fingerprints(count=150, interval=2)["frames"]
        self.store.checkpoint(job, source, -1, {}, [], frames[-1:], revision=1, timeline="archive")
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"url": "fixture", "duration": 300}
        media.archive_fingerprints.side_effect = lambda remote, start, end, **kwargs: [frame for frame in frames if start <= frame["time"] < end]
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        with self.assertRaises(ContinueJob):
            processing.fingerprint_archive(job)
        self.assertEqual(media.archive_fingerprints.call_args.args[1:3], (0, 120))
        restart = Processing(Store(DATABASE), self.config, self.storage, youtube=youtube, media=media)
        with self.assertRaises(ContinueJob):
            restart.fingerprint_archive(job)
        self.assertEqual(media.archive_fingerprints.call_args.args[1:3], (120, 240))
        restart.fingerprint_archive(job)
        self.assertEqual(media.archive_fingerprints.call_args.args[1:3], (240, 298))
        self.assertEqual(self.store.fingerprints(source, "archive", 1)["frames"], frames)

    def test_incomplete_archive_scan_cannot_publish_an_open_match(self):
        self.store.enqueue(
            "recover", f"archive-backfill:{self.source}", broadcast=self.broadcast, source=self.source, delay=60
        )
        job = self.job()
        segment, source, match = self.entities()
        with self.assertRaises(WaitingSource):
            persist_index(self.store, job, segment, source, match, series(), "provisional", True)
        self.store.execute(
            "UPDATE pipeline.segments SET evidence=%s WHERE id=%s", (Jsonb({"closed": True}), self.segment)
        )
        segment, source, match = self.entities()
        self.assertEqual(
            persist_index(self.store, job, segment, source, match, series(), "provisional", True)["state"],
            "provisional",
        )

    def test_incomplete_validation_waits_without_exhausting_retries_then_requires_review(self):
        incomplete = series()[:5]
        self.store.execute("UPDATE pipeline.segments SET rounds=%s WHERE id=%s", (Jsonb(incomplete), self.segment))
        self.store.enqueue("recover", f"archive-backfill:{self.source}", broadcast=self.broadcast, source=self.source, payload={"checkpoint": 200}, delay=3600)
        job = self.job()
        processing = Processing(self.store, self.config, self.storage)
        worker = Worker(self.store, self.config, self.storage, coordinator=Mock(), processing=processing)
        self.store.finish(job, "queued")
        for _ in range(13):
            previous_attempts = self.store.one("SELECT attempt_count FROM pipeline.jobs WHERE id=%s", (job["id"],))["attempt_count"]
            for _ in range(3):
                worker.run_once()
                if self.store.one("SELECT attempt_count FROM pipeline.jobs WHERE id=%s", (job["id"],))["attempt_count"] > previous_attempts:
                    break
            saved = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
            self.assertEqual(saved["state"], "waiting_source")
            self.assertEqual(saved["failure_count"], 0)
            self.assertEqual(self.entities()[0]["state"], "validating")
            self.store.execute("UPDATE pipeline.jobs SET available_at=now() WHERE id=%s", (job["id"],))
        self.store.execute("UPDATE pipeline.jobs SET state='needs_review' WHERE dedupe_key=%s", (f"archive-backfill:{self.source}",))
        for _ in range(3):
            worker.run_once()
            if self.store.one("SELECT state FROM pipeline.jobs WHERE id=%s", (job["id"],))["state"] == "needs_review":
                break
        self.assertEqual(self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))["state"], "needs_review")
        self.assertEqual(self.entities()[0]["state"], "needs_review")
        self.assertFalse(self.store.rows("SELECT * FROM pipeline.match_indexes WHERE state IN ('provisional','final')"))

    def test_pending_backfill_does_not_hide_score_contradiction_or_finished_closed_match(self):
        rounds = series()[:5]
        self.store.execute("UPDATE pipeline.segments SET rounds=%s,findings=%s WHERE id=%s", (Jsonb(rounds), Jsonb([{"code": "impossible_score_change", "map": 1, "round": 5}]), self.segment))
        self.store.enqueue("recover", f"archive-backfill:{self.source}", broadcast=self.broadcast, source=self.source, payload={"checkpoint": 5000}, delay=3600)
        job = self.job()
        processing = Processing(self.store, self.config, self.storage)
        with self.assertRaises(NeedsReview):
            processing.validate(job)
        self.store.execute("UPDATE pipeline.segments SET findings='[]',evidence=%s WHERE id=%s", (Jsonb({"closed": True}), self.segment))
        with self.assertRaises(NeedsReview):
            processing.validate(job)

    def test_matching_targeted_recovery_waits_but_unrelated_recovery_does_not(self):
        self.store.execute("UPDATE pipeline.segments SET rounds=%s,evidence=%s WHERE id=%s", (Jsonb(series()[:5]), Jsonb({"closed": True}), self.segment))
        self.store.enqueue("recover", "targeted-gap", broadcast=self.broadcast, source=self.source, match=self.match, payload={"start": 100, "end": 200}, delay=3600)
        job = self.job()
        processing = Processing(self.store, self.config, self.storage)
        with self.assertRaises(WaitingWork):
            processing.validate(job)
        self.store.execute("UPDATE pipeline.jobs SET payload=%s WHERE dedupe_key='targeted-gap'", (Jsonb({"start": 5000, "end": 5100}),))
        with self.assertRaises(NeedsReview):
            processing.validate(job)

    def test_archive_tail_only_blocks_closed_match_until_coverage_passes_it(self):
        key = f"archive-tail:{self.source}:1"
        self.store.execute("UPDATE pipeline.segments SET evidence=%s WHERE id=%s", (Jsonb({"closed": True}), self.segment))
        self.store.enqueue("recover", key, broadcast=self.broadcast, source=self.source,
                           payload={"start": 100, "end": 10000, "checkpoint": 1200}, delay=3600)
        job = self.job()
        processing = Processing(self.store, self.config, self.storage)
        with self.assertRaises(WaitingWork):
            processing.validate(job)
        self.store.execute("UPDATE pipeline.jobs SET payload=payload||%s WHERE dedupe_key=%s", (Jsonb({"checkpoint": 5000}), key))
        processing.validate(job)
        self.assertEqual(self.store.one("SELECT state FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")["state"], "provisional")
        self.assertEqual(self.store.one("SELECT state FROM pipeline.jobs WHERE dedupe_key=%s", (key,))["state"], "queued")
        self.store.execute("UPDATE pipeline.segments SET evidence=%s WHERE id=%s", (Jsonb({"closed": False}), self.segment))
        with self.assertRaises(WaitingWork):
            processing.validate(job)
        self.store.execute("UPDATE pipeline.segments SET evidence=%s WHERE id=%s", (Jsonb({"closed": True}), self.segment))
        self.store.enqueue("recover", "targeted-gap", broadcast=self.broadcast, source=self.source, match=self.match,
                           payload={"start": 200, "end": 300, "checkpoint": 5000}, delay=3600)
        with self.assertRaises(WaitingWork):
            processing.validate(job)

    def test_old_review_validation_requeues_when_segmentation_detects_pending_coverage(self):
        job = self.job("validate", key=f"validate:{self.segment}:g1", payload={"segment_id": str(self.segment), "segment_revision": 1})
        source = self.entities()[1]
        candidates = [{"accepted": True, "confidence": .99, "detector_version": "fixture", "media_time": value["start"], "round_number": value["round"], "scores": value["scores"], "evidence": {"start": value["start"], "broadcast_map": 1, "teams": ["A", "B"]}} for value in series()[:5]]
        self.store.checkpoint(job, source, 600, {}, candidates, [])
        self.store.finish(job, "needs_review", "Incomplete map")
        self.store.enqueue("recover", f"archive-backfill:{self.source}", broadcast=self.broadcast, source=self.source, payload={"checkpoint": 200}, delay=3600)
        segmentation = self.job("segment")
        processing = Processing(self.store, self.config, self.storage)
        processing.segment(segmentation)
        self.store.finish(segmentation)
        resumed = self.store.claim()
        self.assertEqual(resumed["id"], job["id"])
        self.assertTrue(resumed["payload"]["coverage_pending"])
        with self.assertRaises(WaitingWork):
            processing.validate(resumed)
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.jobs WHERE kind='validate'")), 1)
        self.assertEqual(self.entities()[1]["checkpoint_time"], 600)

    def test_archive_start_override_skips_intro_preserves_timestamps_and_resumes(self):
        from detector import Observation
        import numpy as np

        self.store.execute(
            "UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,)
        )
        config = Config(DATABASE, settings={"archive_start_seconds": {"abcdefghijk": 1800}})
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"url": "fixture", "duration": 2000}
        processing = Processing(self.store, config, self.storage, youtube=youtube, media=media)
        reconciliation = self.job("reconcile")
        processing.reconcile(reconciliation)
        self.store.finish(reconciliation)
        recovery = self.store.one("SELECT * FROM pipeline.jobs WHERE kind='recover'")
        self.assertEqual(recovery["payload"]["start"], 1800)
        self.store.execute(
            "UPDATE pipeline.jobs SET payload=payload||%s WHERE id=%s",
            (Jsonb({"start": 0, "checkpoint": 90}), recovery["id"]),
        )
        job = self.store.claim()
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        media.window.side_effect = lambda remote, start, end, *args, **kwargs: (
            (Observation(time, 1, 100 - (time - 1800), 0.99, scores=(0, 0)), {"teams": ["A", "B"]}, frame)
            for time in [1800, 1801, 1802] if start <= time < end
        )
        media.fingerprint.return_value = {"hash": "ff" * 8}
        with self.assertRaises(ContinueJob):
            processing.recover(job)
        self.assertEqual(media.window.call_args.args[1:3], (1800, 1890))
        accepted = self.store.one("SELECT * FROM pipeline.round_candidates WHERE accepted")
        self.assertEqual(accepted["evidence"]["start"], 1800)
        self.store.finish(job, "queued")
        self.store.execute("UPDATE pipeline.jobs SET priority=10 WHERE id=%s", (job["id"],))
        resumed = self.store.claim()
        with self.assertRaises(ContinueJob):
            processing.recover(resumed)
        self.assertEqual(media.window.call_args.args[1:3], (1890, 1980))

    def test_archive_start_override_does_not_skip_manual_recovery(self):
        config = Config(DATABASE, settings={"archive_start_seconds": {"abcdefghijk": 1800}})
        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready' WHERE id=%s", (self.broadcast,))
        job = self.job("recover", payload={"start": 100, "end": 200, "timeline": "archive"})
        processing = Processing(self.store, config, self.storage)
        processing.recover_official = Mock()
        processing.recover(job)
        self.assertEqual(processing.recover_official.call_args.args[3:5], (100, 200))

    def test_official_recovery_resumes_at_checkpoint_across_yielded_attempts(self):
        from detector import Observation
        import numpy as np

        self.store.execute(
            "UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,)
        )
        job = self.job("recover", payload={"start": 0, "end": 300, "timeline": "archive"})
        self.store.execute("UPDATE pipeline.jobs SET priority=3 WHERE id=%s", (job["id"],))
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"url": "fixture", "duration": 300}
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        media.window.side_effect = lambda remote, start, end, *args, **kwargs: (
            (Observation(time, 1, 100 - int(time), 0.99, scores=(0, 0)), {"teams": ["A", "B"]}, frame)
            for time in [0, 1, 2]
            if start <= time < end
        )
        media.fingerprint.return_value = {"hash": "ff" * 8}
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        source = self.entities()[1]
        mapping = {
            "timelineScale": 1,
            "segments": [{"sourceStart": 0, "sourceEnd": 300, "offset": 0}],
            "anchors": 0,
            "maximumResidual": 0,
        }
        processing.save_alignment(job, source, source, mapping, "reconciliation", 1)
        for checkpoint in [90, 180, 270]:
            with self.assertRaises(ContinueJob):
                processing.recover(job)
            saved = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (job["id"],))
            self.assertEqual(saved["payload"]["checkpoint"], checkpoint)
            self.assertEqual(saved["payload"]["recovered"], 1)
            self.store.finish(job, "queued")
            job = self.store.claim()
        processing.recover(job)
        self.assertEqual(
            [(call.args[1], call.args[2]) for call in media.window.call_args_list],
            [(0, 90), (90, 180), (180, 270), (270, 300)],
        )
        self.assertEqual(
            self.store.one("SELECT count(*) AS count FROM pipeline.round_candidates WHERE accepted")["count"], 1
        )
        gap = [{"round_number": number, "media_time": start + 2, "scores": None,
                "evidence": {"start": start, "broadcast_map": 1}} for number, start in [(7, 100), (9, 200)]]
        with patch.object(processing, "redetect_canonical", return_value=gap), self.assertRaisesRegex(NeedsReview, "did not recover the missing rounds"):
            processing.recover_official(job, self.store.one("SELECT * FROM pipeline.broadcasts WHERE id=%s", (self.broadcast,)), source, 0, 300, {})

    def test_recovery_replaces_unreadable_supporting_samples_without_overwriting_accepted_evidence(self):
        from detector import Observation
        from pipeline.detection import CandidateDetector, redetect_candidates

        job = self.job("recover")
        source = self.entities()[1]
        detector = CandidateDetector()
        unreadable = [detector.observe(Observation(time, None, 100-time, .99)) for time in [0, 1, 2]]
        self.store.checkpoint(job, source, 0, {}, unreadable, [], timeline="archive")
        detector = CandidateDetector()
        readable = [detector.observe(Observation(time, 8, 100-time, .95, scores=(7, 0))) for time in [0, 1, 2]]
        self.store.checkpoint(job, source, 0, {}, readable, [], timeline="archive")
        rows = self.store.rows("SELECT * FROM pipeline.round_candidates ORDER BY media_time")
        detected, _ = redetect_candidates(rows)
        self.assertEqual([candidate["round_number"] for candidate in detected], [8])
        self.store.checkpoint(job, source, 0, {}, unreadable, [], timeline="archive")
        self.assertEqual(self.store.one("SELECT round_number FROM pipeline.round_candidates WHERE accepted")["round_number"], 8)

    def test_existing_canonical_archive_checkpoint_resumes_after_failed_discovery(self):
        self.store.execute(
            "UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,)
        )
        job = self.job("recover", key=f"archive-backfill:{self.source}", payload={"start": 0, "end": 18000, "checkpoint": 7740, "timeline": "archive"})
        source = self.entities()[1]
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"url": "fixture", "duration": 18000}
        media.window.return_value = []
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        processing.save_alignment(job, source, source, {"timelineScale": 1, "segments": [{"sourceStart": 0, "sourceEnd": 18000, "offset": 0}], "anchors": 0, "maximumResidual": 0}, "reconciliation", 1)
        self.store.finish(job, "queued", delay=60)
        channel = {"channel_id": "official"}
        discovery = self.job("discover_youtube", payload={"routes": [{"kind": "associate", "channel": channel}]})
        youtube.discover.side_effect = WaitingSource("Discovery temporarily unavailable")
        with self.assertRaises(WaitingSource):
            Coordinator(self.store, self.config, youtube).discover_youtube(discovery)
        self.store.finish(discovery, "waiting_source", "Discovery temporarily unavailable", 30)
        self.store.execute("UPDATE pipeline.jobs SET available_at=now() WHERE id=%s", (job["id"],))
        resumed = self.store.claim()
        restart = Processing(Store(DATABASE), self.config, self.storage, youtube=youtube, media=media)
        with self.assertRaises(ContinueJob):
            restart.recover(resumed)
        self.assertEqual(media.window.call_args.args[1:3], (7740, 7830))
        self.assertEqual(self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]["checkpoint"], 7830)

    def test_old_archive_detector_state_upgrades_without_rewinding_checkpoint(self):
        from detector import Observation

        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,))
        fixture = json.loads((Path(__file__).parent / "fixtures/pipeline/map_reset.json").read_text())
        job = self.job("recover", key=f"archive-backfill:{self.source}", payload={
            "start": 0, "end": 18000, "checkpoint": 6000, "timeline": "archive",
            "detector_state": {"previous": fixture["previous"], "map_number": 1, "last_time": 5999},
        })
        source = self.entities()[1]
        detector = CandidateDetector()
        candidates = []
        for value in series(base=4000):
            for second in range(3):
                candidate = detector.observe(Observation(value["start"] + second, value["round"], 100 - second, .99), extra={"teams": ["A", "B"]})
                candidate["detector_version"] = "canonical-clock-v1"
                candidates.append(candidate)
        for value in fixture["observations"]:
            candidate = {
                "detector_version": "canonical-clock-v1", "media_time": value["time"],
                "round_number": value["round"], "timer": value["timer"], "scores": None,
                "replay": False, "confidence": value["confidence"], "accepted": False,
                "evidence": value, "findings": ["unconfirmed_map_reset"],
            }
            candidates.append(candidate)
        self.store.checkpoint(job, source, source["checkpoint_time"], {}, candidates, [], timeline="archive")
        original_count = self.store.one("SELECT count(*) AS count FROM pipeline.round_candidates")["count"]
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"url": "refreshed", "duration": 18000}
        media.window.return_value = []
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        processing.save_alignment(job, source, source, {"timelineScale": 1, "segments": [{"sourceStart": 0, "sourceEnd": 18000, "offset": 0}], "anchors": 0, "maximumResidual": 0}, "reconciliation", 1)
        with self.assertRaises(ContinueJob):
            processing.recover(job)
        self.assertEqual(media.window.call_args.args[1:3], (6000, 6090))
        saved = self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]
        self.assertEqual(saved["checkpoint"], 6090)
        self.assertEqual(saved["detector_state"]["map_number"], 2)
        self.assertEqual(saved["detector_state"]["previous"]["round"], 2)
        self.assertEqual(saved["detector_state"]["version"], DETECTOR_VERSION)
        self.assertEqual(self.store.one("SELECT count(*) AS count FROM pipeline.round_candidates")["count"], original_count)

    def test_stale_lease_recovery_records_attempt(self):
        job = self.job()
        self.store.execute("UPDATE pipeline.jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (job["id"],))
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.one("SELECT failure_kind FROM pipeline.jobs WHERE id=%s", (job["id"],))["failure_kind"], "worker_crash")
        self.store.execute("UPDATE pipeline.jobs SET available_at=now() WHERE id=%s", (job["id"],))
        reclaimed = self.store.claim()
        self.assertEqual(reclaimed["id"], job["id"])
        self.assertNotEqual(reclaimed["lease_token"], job["lease_token"])
        attempts = self.store.rows("SELECT * FROM pipeline.attempts WHERE job_id=%s ORDER BY number", (job["id"],))
        self.assertEqual([item["state"] for item in attempts], ["expired", "running"])

    def test_stale_worker_cannot_overwrite_newer_index(self):
        old = self.job()
        self.store.execute("UPDATE pipeline.jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (old["id"],))
        self.assertIsNone(self.store.claim())
        self.store.execute("UPDATE pipeline.jobs SET available_at=now() WHERE id=%s", (old["id"],))
        fresh = self.store.claim()
        segment, source, match = self.entities()
        newest = persist_index(self.store, fresh, segment, source, match, series(), "provisional", True)
        with self.assertRaises(StaleAttempt):
            persist_index(
                self.store,
                old,
                segment,
                source,
                match,
                [{**item, "start": item["start"] - 15} for item in series()],
                "provisional",
                True,
            )
        stored = self.store.one("SELECT * FROM pipeline.match_indexes WHERE id=%s", (newest["id"],))
        self.assertEqual(stored["rounds"][0]["start"], 100)

    def test_generation_change_fences_old_attempt(self):
        job = self.job()
        self.store.execute("UPDATE pipeline.broadcasts SET generation=2 WHERE id=%s", (self.broadcast,))
        with self.assertRaises(StaleAttempt):
            persist_index(self.store, job, *self.entities(), series(), "provisional", True)

    def test_reenqueued_generation_closes_superseded_attempt(self):
        old = self.job(key="same-work")
        self.store.execute("UPDATE pipeline.broadcasts SET generation=2 WHERE id=%s", (self.broadcast,))
        self.store.enqueue("validate", "same-work", broadcast=self.broadcast, source=self.source)
        fresh = self.store.claim()
        self.assertEqual(fresh["id"], old["id"])
        self.assertEqual(fresh["generation"], 2)
        attempts = self.store.rows("SELECT * FROM pipeline.attempts WHERE job_id=%s ORDER BY number", (old["id"],))
        self.assertEqual([item["state"] for item in attempts], ["unsupported", "running"])
        with self.assertRaises(StaleAttempt):
            self.store.finish(old)

    def test_manual_assignment_survives_resegmentation_without_clearing_gaps(self):
        self.store.execute(
            "UPDATE pipeline.segments SET evidence=%s WHERE id=%s",
            (Jsonb({"manual_assignment": "Verified broadcast matchup graphic"}), self.segment),
        )
        detected = {
            "match": None,
            "state": "needs_review",
            "start": 100,
            "end": 1300,
            "rounds": series(),
            "findings": [{"code": "unassigned_match"}, {"code": "missing_rounds"}],
            "evidence": {"detected_order": 1, "closed": False},
        }
        job = self.job("segment")
        with patch("pipeline.processing.segment_matches", return_value=[detected]):
            Processing(self.store, self.config, self.storage).segment(job)
        stored, _, _ = self.entities()
        self.assertEqual(stored["expected_match_id"], self.match)
        self.assertEqual(stored["evidence"]["manual_assignment"], "Verified broadcast matchup graphic")
        self.assertEqual(stored["findings"], [{"code": "missing_rounds"}])
        self.assertEqual(
            self.store.one("SELECT * FROM pipeline.jobs WHERE kind='validate'")["expected_match_id"], self.match
        )

    def test_failed_segment_reconciliation_does_not_block_healthy_match(self):
        self.store.execute(
            "UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,)
        )
        capture = self.job("live")
        _, source, _ = self.entities()
        self.store.checkpoint(capture, source, 1400, {}, [], fingerprints(count=145)["frames"])
        self.store.checkpoint(
            capture, source, 1400, {}, [], fingerprints(count=145)["frames"], revision=1, timeline="archive"
        )
        self.store.finish(capture)
        failed_match, failed_segment = identifier(), identifier()
        self.store.execute(
            """INSERT INTO pipeline.expected_matches(id,broadcast_id,event,stage,day,team_a,team_b,match_order,best_of,region,channel_id,provider,external_id,completion)
                           VALUES (%s,%s,'Champions','Groups','2026-10-04','C','D',2,1,'international','official','manual','failed-match','completed')""",
            (failed_match, self.broadcast),
        )
        self.store.execute(
            """INSERT INTO pipeline.segments(id,broadcast_id,expected_match_id,generation,start_time,end_time,state,rounds,findings)
                           VALUES (%s,%s,%s,1,3000,4200,'needs_review',%s,%s)""",
            (
                failed_segment,
                self.broadcast,
                failed_match,
                Jsonb(series(base=3000)),
                Jsonb([{"code": "missing_rounds"}]),
            ),
        )
        processing = Processing(self.store, self.config, self.storage)
        failed = self.job("reconcile_segment", payload={"segment_id": str(failed_segment), "revision": 1})
        failed["expected_match_id"] = failed_match
        with self.assertRaises(NeedsReview):
            processing.reconcile_segment(failed)
        self.store.finish(failed, "needs_review", "Missing rounds")
        healthy = self.job("reconcile_segment", payload={"segment_id": str(self.segment), "revision": 1})
        processing.reconcile_segment(healthy)
        stored = self.store.one("SELECT * FROM pipeline.match_indexes WHERE expected_match_id=%s", (self.match,))
        self.assertEqual(stored["state"], "final")
        self.assertEqual(stored["provenance"]["method"], "segment_archive_reconciled")

    def test_stale_segment_snapshot_cannot_replace_newer_success(self):
        old = self.job()
        segment, source, match = self.entities()
        self.store.execute(
            "UPDATE pipeline.segments SET rounds=%s,revision=revision+1 WHERE id=%s",
            (Jsonb([{**item, "start": item["start"] + 1} for item in series()]), self.segment),
        )
        new = old
        current, _, _ = self.entities()
        persist_index(self.store, new, current, source, match, current["rounds"], "provisional", True)
        with self.assertRaisesRegex(StaleAttempt, "Segment changed while validation was running"):
            persist_index(self.store, old, segment, source, match, segment["rounds"], "provisional", True)
        self.assertEqual(
            self.store.one("SELECT * FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")["rounds"][0]["start"],
            101,
        )

    def test_checkpoint_and_candidates_survive_new_store(self):
        from detector import Observation

        job = self.job("live")
        _, source, _ = self.entities()
        detector = CandidateDetector()
        candidates = [detector.observe(Observation(100 + second, 1, 100 - second, 0.99)) for second in range(2)]
        self.store.checkpoint(job, source, 101, detector.state(), candidates, [])
        resumed_store = Store(DATABASE)
        saved = resumed_store.one("SELECT * FROM pipeline.sources WHERE id=%s", (self.source,))
        resumed = CandidateDetector(saved["detector_state"])
        candidate = resumed.observe(Observation(102, 1, 98, 0.99))
        resumed_store.checkpoint(job, saved, 102, resumed.state(), [candidate], [])
        resumed_store.checkpoint(job, saved, 102, resumed.state(), [candidate], [])
        self.assertEqual(self.store.one("SELECT count(*) AS count FROM pipeline.round_candidates")["count"], 3)
        self.assertEqual(
            self.store.one("SELECT count(*) AS count FROM pipeline.round_candidates WHERE accepted")["count"], 1
        )

    def test_provisional_publication_and_shadow_export(self):
        job = self.job()
        processing = Processing(self.store, self.config, self.storage)
        processing.validate(job)
        catalog = export_snapshot(self.store, self.storage)
        self.assertEqual(catalog["videos"][0]["tournament"], "Champions")
        self.assertEqual(catalog["videos"][0]["tournamentKey"], "champions")
        self.assertEqual(len(catalog["videos"]), 1)
        self.assertEqual(catalog["videos"][0]["pipelineState"], "provisional")
        path = "shadow/" + catalog["videos"][0]["index"].lstrip("/")
        index = json.loads(self.storage.get(path))
        self.assertEqual(index["sourceId"], "abcdefghijk")
        self.assertEqual(index["schemaVersion"], 2)
        self.assertEqual(index["rounds"][0]["start"], 100)
        self.assertFalse((Path(self.temporary.name) / "release").exists())

    def test_watchparty_export_uses_own_chat_and_withholds_only_unaligned_variant(self):
        job = self.job()
        persist_index(self.store, job, *self.entities(), series(), "provisional", False)
        self.store.finish(job)
        secondary = identifier()
        self.store.execute("""INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,state,checkpoint_time,metadata)
                            VALUES (%s,%s,'twitch','123456789','watch_party','ended',1700,%s)""",
                           (secondary, self.broadcast, Jsonb({"vod_id": "123456789", "login": "gofns"})))
        site = Path(self.temporary.name) / "site"
        site.mkdir()
        held = export_snapshot(self.store, self.storage, False, ["abcdefghijk"])
        legacy = {"provider": "twitch", "sourceId": "123456789", "catalogId": held["withheld"][0]["catalogId"], "title": "A vs B"}
        (site / "catalog.json").write_text(json.dumps({"videos": [legacy]}))
        release_to_site(Path(self.temporary.name) / "release", site)
        installed = json.loads((site / "catalog.json").read_text())
        self.assertEqual(len(installed["videos"]), 1)
        self.assertTrue(installed["withheld"][0]["canonicalPipeline"])
        mapping = {"timelineScale": 1, "anchors": 12, "maximumResidual": .1, "segments": [
            {"sourceStart": 100, "sourceEnd": 1700, "canonicalStart": 0, "canonicalEnd": 1600,
             "offset": -100, "anchors": 12, "maximumResidual": .1}]}
        alignment_job = self.job("twitch_align")
        processing = Processing(self.store, self.config, self.storage)
        source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (secondary,))
        processing.save_alignment(alignment_job, source, self.entities()[1], mapping, "secondary", 0)
        processing.save_alignment(alignment_job, source, self.entities()[1], mapping, "secondary", 0)
        self.assertEqual(self.store.one("SELECT revision FROM pipeline.alignments WHERE source_id=%s", (secondary,))["revision"], 1)
        self.store.execute("""INSERT INTO pipeline.chat_messages(source_id,message_id,media_time,wall_time,origin,message)
                            VALUES (%s,'message',200,'2026-10-04T10:03:20Z','vod',%s)""",
                           (secondary, Jsonb({"content_offset_seconds": 200, "commenter": {"display_name": "Viewer"},
                                            "message": {"body": "Hello", "fragments": [{"text": "Hello"}], "user_color": ""}})))
        catalog = export_snapshot(self.store, self.storage, False, ["abcdefghijk"])
        official, watchparty = catalog["videos"]
        self.assertEqual(official["provider"], "youtube")
        self.assertNotIn("chat", official)
        self.assertEqual(watchparty["provider"], "twitch")
        self.assertEqual(watchparty["creator"], "FNS")
        self.assertEqual(watchparty["chatSourceId"], "123456789")
        contract = json.loads(self.storage.get("release/" + watchparty["index"].lstrip("/")))
        self.assertEqual(contract["rounds"][0]["start"], 200)
        self.assertEqual(contract["canonical"]["rounds"][0]["start"], 100)
        release_to_site(Path(self.temporary.name) / "release", site)
        self.assertEqual(len(json.loads((site / "catalog.json").read_text())["videos"]), 2)
        old_catalog = catalog
        changed = {**mapping, "segments": [{**mapping["segments"][0], "sourceStart": 110, "sourceEnd": 1710, "offset": -110}]}
        processing.save_alignment(alignment_job, source, self.entities()[1], changed, "secondary", 0)
        catalog = export_snapshot(self.store, self.storage, False, ["abcdefghijk"])
        release_to_site(Path(self.temporary.name) / "release", site)
        self.assertEqual(catalog["videos"][1]["pipelineAlignmentVersion"], 2)
        self.storage.put("release/catalog.json", json_bytes(old_catalog))
        with self.assertRaisesRegex(ValueError, "older"):
            release_to_site(Path(self.temporary.name) / "release", site)
        self.storage.put("release/catalog.json", json_bytes(catalog))
        path = "release/" + catalog["videos"][1]["index"].lstrip("/")
        contents = self.storage.get(path)
        invalid = json.loads(contents)
        invalid["rounds"][0]["start"] += 1
        self.storage.put(path, json_bytes(invalid))
        with self.assertRaisesRegex(ValueError, "canonical timestamps"):
            release_to_site(Path(self.temporary.name) / "release", site)
        self.storage.put(path, contents)
        self.store.execute("UPDATE pipeline.sources SET revision=revision+1 WHERE id=%s", (secondary,))
        catalog = export_snapshot(self.store, self.storage, False, ["abcdefghijk"])
        self.assertEqual(len(catalog["videos"]), 1)
        self.assertEqual(catalog["withheld"][0]["provider"], "twitch")
        release_to_site(Path(self.temporary.name) / "release", site)
        self.assertEqual(len(json.loads((site / "catalog.json").read_text())["videos"]), 1)
        self.assertEqual(self.store.one("SELECT checkpoint_time FROM pipeline.sources WHERE id=%s", (secondary,))["checkpoint_time"], 1700)
        source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (secondary,))
        processing.save_alignment(alignment_job, source, self.entities()[1], changed, "secondary", 0)
        export_snapshot(self.store, self.storage, False, ["abcdefghijk"])
        release_to_site(Path(self.temporary.name) / "release", site)
        self.store.execute("UPDATE pipeline.segments SET state='needs_review' WHERE id=%s", (self.segment,))
        export_snapshot(self.store, self.storage, False, ["abcdefghijk"])
        release_to_site(Path(self.temporary.name) / "release", site)
        self.assertEqual(json.loads((site / "catalog.json").read_text())["videos"], [])
        self.assertEqual(self.store.one("SELECT state FROM pipeline.watchparty_publications WHERE source_id=%s", (secondary,))["state"], "needs_review")

    def test_release_only_exports_approved_current_validated_segments(self):
        job = self.job()
        segment, source, match = self.entities()
        persist_index(self.store, job, segment, source, match, series(), "provisional", False)
        self.assertEqual(export_snapshot(self.store, self.storage, False, ["another-id"])["videos"], [])
        self.assertEqual(len(export_snapshot(self.store, self.storage, False, ["abcdefghijk"])["videos"]), 1)
        self.store.execute("UPDATE pipeline.segments SET revision=revision+1 WHERE id=%s", (self.segment,))
        self.assertEqual(export_snapshot(self.store, self.storage, False, ["abcdefghijk"])["videos"], [])
        self.store.execute("UPDATE pipeline.segments SET revision=revision-1,state='needs_review' WHERE id=%s", (self.segment,))
        self.assertEqual(export_snapshot(self.store, self.storage, False, ["abcdefghijk"])["videos"], [])

    def test_export_withholds_bad_match_and_keeps_healthy_match(self):
        job = self.job()
        segment, source, match = self.entities()
        persist_index(self.store, job, segment, source, match, series(), "provisional", False)
        self.store.finish(job)
        bad_broadcast, bad_source, bad_match, bad_segment = (identifier() for _ in range(4))
        self.store.execute("INSERT INTO pipeline.broadcasts(id,event,day,region,channel_id,youtube_id,state,seekable) VALUES (%s,'Champions','2026-10-04','international','official','badsource01','archive_ready',true)", (bad_broadcast,))
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'youtube','badsource01','canonical')", (bad_source, bad_broadcast))
        self.store.execute("INSERT INTO pipeline.expected_matches(id,broadcast_id,event,stage,day,team_a,team_b,match_order,best_of,region,channel_id,provider,external_id,completion) VALUES (%s,%s,'Champions','Groups','2026-10-04','C','D',2,1,'international','official','manual','bad-match','completed')", (bad_match, bad_broadcast))
        self.store.execute("INSERT INTO pipeline.segments(id,broadcast_id,expected_match_id,generation,start_time,end_time,state,rounds) VALUES (%s,%s,%s,1,100,1300,'validating',%s)", (bad_segment, bad_broadcast, bad_match, Jsonb(series())))
        self.store.enqueue("validate", "bad-match", broadcast=bad_broadcast, source=bad_source, match=bad_match)
        bad_job = self.store.claim()
        bad_entities = [self.store.one(f"SELECT * FROM pipeline.{table} WHERE id=%s", (key,)) for table, key in [("segments", bad_segment), ("sources", bad_source), ("expected_matches", bad_match)]]
        persist_index(self.store, bad_job, *bad_entities, series(), "provisional", False)
        catalog = export_snapshot(self.store, self.storage, False, ["abcdefghijk", "badsource01"])
        self.assertEqual([value["expectedMatchId"] for value in catalog["videos"]], [str(self.match)])
        self.assertEqual([value["expectedMatchId"] for value in catalog["withheld"]], [str(bad_match)])

    def test_live_playback_clock_export_preserves_wall_time_and_chat_mapping(self):
        job = self.job()
        segment, source, match = self.entities()
        shift = 7.793
        rounds = [{**item, "start": item["start"] + shift} for item in series()]
        persist_index(self.store, job, segment, source, match, rounds, "provisional", True,
                      provenance={"method": "live_presentation_clock", "clock": {"playback_shift": shift}})
        twitch = identifier()
        self.store.execute(
            "INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'twitch','123456789','official_twitch')",
            (twitch, self.broadcast),
        )
        mapping = {"timelineScale": 1, "segments": [{"offset": 10, "canonicalStart": 100, "canonicalEnd": 1400}]}
        self.store.execute(
            "INSERT INTO pipeline.alignments(id,source_id,canonical_source_id,source_revision,canonical_revision,kind,mapping,diagnostics) VALUES (%s,%s,%s,1,0,'secondary',%s,'{}')",
            (identifier(), twitch, self.source, Jsonb(mapping)),
        )
        self.store.execute(
            "INSERT INTO pipeline.chat_messages(source_id,message_id,media_time,wall_time,origin,message) VALUES (%s,'message',90,now(),'live',%s)",
            (twitch, Jsonb({"user": "Viewer", "message": {"fragments": [{"text": "hello"}]}})),
        )
        catalog = export_snapshot(self.store, self.storage)
        entry = catalog["videos"][0]
        index = json.loads(self.storage.get("shadow/" + entry["index"].lstrip("/")))
        self.assertEqual(timestamp(entry["playedAt"]), source["actual_start"] + timedelta(seconds=100))
        self.assertEqual(index["leadSeconds"], 5)
        self.assertAlmostEqual(index["rounds"][0]["start"] - index["leadSeconds"], 102.793)
        self.assertEqual(index["alignment"]["segments"][0], {"offset": -17.793, "targetStart": 107.793, "targetEnd": 1407.793})
        self.assertEqual(self.store.one("SELECT mapping FROM pipeline.alignments")["mapping"], mapping)
        self.store.execute("UPDATE pipeline.sources SET metadata=%s WHERE id=%s", (Jsonb({"vod_id": "987654321"}), twitch))
        entry = export_snapshot(self.store, self.storage)["videos"][0]
        self.assertEqual(entry["chatSourceId"], "987654321")
        self.assertEqual(json.loads(self.storage.get("shadow/chats/twitch-987654321.json"))["source"], "987654321")
        self.store.execute("UPDATE pipeline.sources SET role='watch_party' WHERE id=%s", (twitch,))
        entry = export_snapshot(self.store, self.storage)["videos"][0]
        self.assertNotIn("chat", entry)
        self.assertNotIn("chatSourceId", entry)

    def test_release_blocks_upload_contradiction_without_discarding_index(self):
        job = self.job()
        segment, source, match = self.entities()
        persist_index(self.store, job, segment, source, match, series(), "provisional", False)
        self.store.enqueue("validate_upload", "upload-check", broadcast=self.broadcast, source=self.source, match=self.match)
        self.store.execute("UPDATE pipeline.jobs SET state='needs_review' WHERE dedupe_key='upload-check'")
        self.assertEqual(export_snapshot(self.store, self.storage, False, ["abcdefghijk"])["videos"], [])
        self.assertEqual(self.store.one("SELECT state FROM pipeline.match_indexes")["state"], "provisional")
        self.store.execute("UPDATE pipeline.segments SET evidence=%s WHERE id=%s", (Jsonb({"full_match_validation": {"missing_rounds": [[2, 1]], "extra_rounds": []}}), self.segment))
        self.store.execute("UPDATE pipeline.jobs SET state='queued' WHERE dedupe_key='upload-check'")
        self.assertEqual(export_snapshot(self.store, self.storage, False, ["abcdefghijk"])["videos"], [])
        self.store.execute("UPDATE pipeline.segments SET evidence=%s WHERE id=%s", (Jsonb({"full_match_validation": {"missing_rounds": [], "extra_rounds": []}}), self.segment))
        self.store.execute("UPDATE pipeline.jobs SET state='succeeded' WHERE dedupe_key='upload-check'")
        self.assertEqual(len(export_snapshot(self.store, self.storage, False, ["abcdefghijk"])["videos"]), 1)

    def test_incomplete_upload_subset_does_not_veto_complete_canonical_index(self):
        self.store.execute("UPDATE pipeline.expected_matches SET best_of=3 WHERE id=%s", (self.match,))
        self.store.execute("UPDATE pipeline.segments SET rounds=%s,end_time=4300 WHERE id=%s", (Jsonb(series(maps=2)), self.segment))
        canonical_job = self.job()
        segment, source, match = self.entities()
        persist_index(self.store, canonical_job, segment, source, match, series(maps=2), "provisional", False)
        self.store.finish(canonical_job)
        upload = identifier()
        self.store.execute("INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role,checkpoint_time) VALUES (%s,%s,'youtube','fullupload','full_match',1300)", (upload, self.broadcast))
        self.store.enqueue("validate_upload", "incomplete-upload", broadcast=self.broadcast, source=upload, match=self.match)
        job = self.store.claim()
        upload_source = self.store.one("SELECT * FROM pipeline.sources WHERE id=%s", (upload,))
        candidates = [{"accepted": True, "confidence": .99, "detector_version": "fixture", "media_time": value["start"], "round_number": value["round"], "scores": value["scores"], "evidence": {"start": value["start"], "broadcast_map": value["map"]}} for value in series()]
        self.store.checkpoint(job, upload_source, 1300, {}, candidates, [], timeline="archive")
        youtube, media = Mock(), Mock()
        youtube.resolve.return_value = {"duration": 1301, "url": "fixture"}
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        processing.validate_upload(job)
        self.store.finish(job)
        comparison = self.entities()[0]["evidence"]["full_match_validation"]
        self.assertEqual(comparison["status"], "source_incomplete")
        self.assertEqual(self.store.one("SELECT state FROM pipeline.jobs WHERE id=%s", (job["id"],))["state"], "succeeded")
        self.assertEqual(comparison["missing_rounds"], [])
        self.assertTrue(comparison["extra_rounds"])
        self.assertEqual(len(export_snapshot(self.store, self.storage, False, ["abcdefghijk"])["videos"]), 1)
        media.window.assert_not_called()
        self.store.execute("UPDATE pipeline.segments SET evidence=%s WHERE id=%s", (Jsonb({"full_match_validation": {**comparison, "status": "source_needs_review", "source_findings": [{"code": "impossible_score_change"}]}}), self.segment))
        self.assertEqual(len(export_snapshot(self.store, self.storage, False, ["abcdefghijk"])["videos"]), 1)
        for changes in [{"missing_rounds": [[1, 14]]}, {"verified_contradiction": True}, {"status": "contradiction"}, {"status": "source_needs_review", "verified_contradiction": True}]:
            self.store.execute("UPDATE pipeline.segments SET evidence=%s WHERE id=%s", (Jsonb({"full_match_validation": {**comparison, **changes}}), self.segment))
            self.assertEqual(export_snapshot(self.store, self.storage, False, ["abcdefghijk"])["videos"], [])

    def test_terminal_map_gap_schedules_archive_tail_without_resetting_capture(self):
        from detector import DETECTOR_VERSION as OCR_VERSION

        self.store.execute("UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1,reconciled_revision=1 WHERE id=%s", (self.broadcast,))
        self.store.execute("UPDATE pipeline.sources SET checkpoint_time=777,detector_state=%s WHERE id=%s", (Jsonb({"version": "stored"}), self.source))
        capture = self.job("live")
        source = self.entities()[1]
        candidates = [{"accepted": True, "confidence": .99, "detector_version": "fixture", "media_time": value["start"], "round_number": value["round"], "scores": value["scores"], "evidence": {"start": value["start"], "broadcast_map": value["map"], "teams": value["teams"]}} for value in series()[:5]]
        self.store.checkpoint(capture, source, 777, source["detector_state"], candidates, [{"time": 2000, "hash": "00" * 8}], revision=1, timeline="archive")
        processing = Processing(self.store, self.config, self.storage)
        processing.save_alignment(capture, source, source, {"timelineScale": 1, "segments": [{"sourceStart": 0, "sourceEnd": 2000, "offset": 0}], "anchors": 30, "maximumResidual": 0}, "reconciliation", 1)
        self.store.finish(capture)
        segmentation = self.job("segment")
        processing.segment(segmentation)
        processing.segment(segmentation)
        recovery = self.store.rows("SELECT * FROM pipeline.jobs WHERE kind='recover'")
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0]["payload"], {"start": 490, "end": 2002, "stage": 2, "timeline": "archive", "terminal_map": 1})
        self.assertTrue(recovery[0]["dedupe_key"].endswith(OCR_VERSION))
        progress = {**recovery[0]["payload"], "checkpoint": 1000, "detector_state": {"version": DETECTOR_VERSION}, "recovered": 1}
        self.store.execute("UPDATE pipeline.jobs SET payload=%s WHERE id=%s", (Jsonb(progress), recovery[0]["id"]))
        value = series()[5]
        candidate = {"accepted": True, "confidence": .99, "detector_version": "fixture", "media_time": value["start"],
                     "round_number": value["round"], "scores": value["scores"],
                     "evidence": {"start": value["start"], "broadcast_map": value["map"], "teams": value["teams"]}}
        self.store.checkpoint(segmentation, source, 777, source["detector_state"], [candidate], [], revision=1, timeline="archive")
        processing.segment(segmentation)
        continued = self.store.rows("SELECT * FROM pipeline.jobs WHERE kind='recover'")
        self.assertEqual(len(continued), 1)
        self.assertEqual(continued[0]["id"], recovery[0]["id"])
        self.assertEqual(continued[0]["state"], "queued")
        self.assertEqual(continued[0]["payload"], progress)
        self.assertEqual(self.entities()[1]["checkpoint_time"], 777)
        self.assertEqual(self.entities()[1]["detector_state"], {"version": "stored"})

    def test_repeated_segmentation_preserves_upload_validation_without_duplicate_jobs(self):
        capture = self.job("live")
        candidates = [{"accepted": True, "confidence": .99, "detector_version": "fixture", "media_time": value["start"], "round_number": value["round"], "scores": value["scores"], "evidence": {"start": value["start"], "broadcast_map": value["map"], "teams": value["teams"]}} for value in series()]
        self.store.checkpoint(capture, self.entities()[1], 1300, {}, candidates, [])
        self.store.finish(capture)
        segmentation = self.job("segment")
        processing = Processing(self.store, self.config, self.storage)
        processing.segment(segmentation)
        revision = self.entities()[0]["revision"]
        for comparison in [{"status": "source_incomplete", "missing_rounds": [], "extra_rounds": [[2, 1]], "segment_revision": revision},
                           {"status": "contradiction", "missing_rounds": [[1, 14]], "verified_contradiction": True, "segment_revision": revision}]:
            self.store.execute("UPDATE pipeline.segments SET evidence=evidence||%s WHERE id=%s", (Jsonb({"full_match_validation": comparison}), self.segment))
            processing.segment(segmentation)
            processing.segment(segmentation)
            saved = self.entities()[0]
            self.assertEqual(saved["evidence"]["full_match_validation"], comparison)
            self.assertEqual(saved["revision"], revision)
            self.assertEqual(len(self.store.rows("SELECT id FROM pipeline.jobs WHERE kind='validate'")), 1)
        self.assertEqual(self.entities()[1]["checkpoint_time"], 1300)

    def test_targeted_job_claim_keeps_normal_lease_and_broadcast_exclusion(self):
        self.store.enqueue("export", "older-export", broadcast=self.broadcast, priority=100)
        self.store.enqueue("segment", "target-segment", broadcast=self.broadcast)
        target = self.store.one("SELECT id FROM pipeline.jobs WHERE dedupe_key='target-segment'")["id"]
        job = self.store.claim(job_id=target)
        self.assertEqual(job["id"], target)
        self.assertIsNone(self.store.claim(job_id=target))
        self.assertIsNone(self.store.claim())
        self.store.finish(job)
        next_job = self.store.claim()
        self.assertEqual(next_job["dedupe_key"], "older-export")

    def test_new_review_version_does_not_export_older_success(self):
        job = self.job()
        segment, source, match = self.entities()
        persist_index(self.store, job, segment, source, match, series(), "provisional", True)
        persist_index(self.store, job, segment, source, match, series(), "needs_review", True, findings=[{"code": "score_conflict"}])
        self.assertEqual(export_snapshot(self.store, self.storage)["videos"], [])

    def test_prepare_rollout_preserves_worker_configuration_and_checkpoints(self):
        config_path = Path(self.temporary.name) / "worker.json"
        settings = {"processing_slots": 2, "approved_broadcasts": [], "youtube_channels": [{"channel_id": "official"}]}
        config_path.write_text(json.dumps(settings))
        output = Path(self.temporary.name) / "rollout.json"
        self.store.execute("UPDATE pipeline.sources SET checkpoint_time=5850,detector_state=%s WHERE id=%s", (Jsonb({"map_number": 3}), self.source))
        before = self.entities()[1]
        arguments = ["pipeline.cli", "--config", str(config_path), "prepare-rollout", "abcdefghijk", "--day", "2026-10-04", "--output", str(output)]
        with patch.dict(os.environ, {"DATABASE_URL": DATABASE, "SPOILLESS_PIPELINE_MODE": "shadow"}), patch.object(sys, "argv", arguments), patch("builtins.print"):
            cli_main()
            self.assertEqual(json.loads(output.read_text())["approved_broadcasts"], ["abcdefghijk"])
            self.assertEqual(json.loads(config_path.read_text()), settings)
            self.assertEqual(self.entities()[1], before)
            arguments[-1] = str(config_path)
            with self.assertRaisesRegex(ValueError, "separate rollout"):
                cli_main()
            arguments[-1] = str(output)
            arguments[-3] = "2026-10-05"
            with self.assertRaisesRegex(ValueError, "explicitly approved day"):
                cli_main()

    def test_upcoming_youtube_uses_scheduled_release_day_without_api_key(self):
        youtube = Mock()
        youtube.metadata.return_value = {"state": "scheduled", "actual_start": None, "actual_end": None, "metadata": {"release_timestamp": 1791363600}}
        job = self.job("associate", payload={"entry": {"id": "upcoming123", "published": "2026-10-06T10:00:00Z"}, "channel": {"channel_id": "official", "region": "international", "timezone": "Asia/Shanghai"}})
        Coordinator(self.store, self.config, youtube=youtube).associate(job)
        broadcast = self.store.one("SELECT * FROM pipeline.broadcasts WHERE youtube_id='upcoming123'")
        self.assertEqual(str(broadcast["day"]), "2026-10-07")
        self.assertEqual(broadcast["state"], "scheduled")
        self.assertIsNone(broadcast["actual_start"])
        self.store.finish(job)
        self.store.execute("UPDATE pipeline.broadcasts SET day='2026-10-05' WHERE id=%s", (broadcast["id"],))
        self.store.execute("UPDATE pipeline.expected_matches SET day='2026-10-07',broadcast_id=NULL WHERE id=%s", (self.match,))
        self.store.enqueue("refresh", "refresh-upcoming", broadcast=broadcast["id"])
        refresh = self.store.claim()
        Coordinator(self.store, self.config, youtube=youtube).refresh(refresh)
        corrected = self.store.one("SELECT * FROM pipeline.broadcasts WHERE id=%s", (broadcast["id"],))
        self.assertEqual(str(corrected["day"]), "2026-10-07")
        self.assertEqual(self.entities()[2]["broadcast_id"], broadcast["id"])
        saved = self.entities()[1]
        self.assertEqual(saved["checkpoint_time"], -1)
        self.assertEqual(saved["broadcast_id"], self.broadcast)
        upcoming_source = self.store.one("SELECT id FROM pipeline.sources WHERE broadcast_id=%s", (broadcast["id"],))
        self.store.execute("UPDATE pipeline.sources SET checkpoint_time=5850 WHERE id=%s", (upcoming_source["id"],))
        self.store.execute("UPDATE pipeline.broadcasts SET day='2026-10-05' WHERE id=%s", (broadcast["id"],))
        Coordinator(self.store, self.config, youtube=youtube).refresh(refresh)
        self.assertEqual(str(self.store.one("SELECT day FROM pipeline.broadcasts WHERE id=%s", (broadcast["id"],))["day"]), "2026-10-05")
        self.assertEqual(self.store.one("SELECT checkpoint_time FROM pipeline.sources WHERE id=%s", (upcoming_source["id"],))["checkpoint_time"], 5850)

    def test_delayed_seekability_freezes_without_repeating_ocr(self):
        self.store.execute("UPDATE pipeline.broadcasts SET state='ended_waiting_archive',seekable=false WHERE id=%s", (self.broadcast,))
        job = self.job()
        processing = Processing(self.store, self.config, self.storage, media=Mock())
        with self.assertRaises(WaitingSource):
            processing.validate(job)
        self.assertEqual(self.store.one("SELECT * FROM pipeline.match_indexes")["state"], "validating")
        self.store.execute("UPDATE pipeline.broadcasts SET seekable=true WHERE id=%s", (self.broadcast,))
        processing.validate(job)
        self.assertEqual(
            self.store.one("SELECT * FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")["state"], "provisional"
        )
        processing.media.window.assert_not_called()

    def test_live_dvr_verification_publishes_without_repeating_ocr_or_resetting_checkpoint(self):
        self.store.execute("UPDATE pipeline.broadcasts SET seekable=false WHERE id=%s", (self.broadcast,))
        self.store.execute("UPDATE pipeline.sources SET checkpoint_time=5850 WHERE id=%s", (self.source,))
        job = self.job()
        source = self.entities()[1]
        media = Mock()
        media.manifest.return_value = [
            {"wall_time": source["actual_start"] + timedelta(seconds=point), "duration": 5}
            for point in range(0, 1400, 5)
        ]
        media.live_presentation_time.side_effect = lambda remote, item: (item["wall_time"] - source["actual_start"]).total_seconds() + 7.793
        youtube = Mock()
        youtube.resolve.return_value = {"url": "fixture"}
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        processing.validate(job)
        self.assertEqual(self.store.one("SELECT state FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")["state"], "provisional")
        self.assertEqual(self.entities()[1]["checkpoint_time"], 5850)
        media.manifest.assert_called_once_with({"url": "fixture"}, start_sequence=0)
        self.assertEqual(media.live_presentation_time.call_count, 3)
        media.window.assert_not_called()
        media.observation.assert_not_called()
        verified = self.entities()[1]["metadata"]["live_dvr_range"]
        self.assertEqual((verified["start"], verified["end"], verified["revision"], verified["playback_shift"]), (85, 1300, source["revision"], 7.793))
        index = self.store.one("SELECT * FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")
        self.assertEqual(index["rounds"], [{**item, "start": item["start"] + 7.793} for item in series()])
        self.assertEqual(self.entities()[0]["rounds"], series())
        processing.validate(job)
        self.assertEqual(self.store.one("SELECT * FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")["id"], index["id"])
        self.assertEqual(media.live_presentation_time.call_count, 3)
        self.store.execute("UPDATE pipeline.segments SET rounds=%s,revision=revision+1 WHERE id=%s", (Jsonb(series(base=2100)), self.segment))
        with self.assertRaises(WaitingWork):
            processing.validate(job)
        self.assertEqual(media.manifest.call_count, 2)
        self.assertEqual(self.store.one("SELECT state FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")["state"], "validating")

    def test_live_dvr_missing_coverage_and_decode_failures_do_not_publish(self):
        for failure in ["short_window", "gap", "missing_anchor", "empty_frames", "403"]:
            with self.subTest(failure=failure):
                self.store.execute("UPDATE pipeline.broadcasts SET seekable=false WHERE id=%s", (self.broadcast,))
                self.store.execute("UPDATE pipeline.sources SET checkpoint_time=5850 WHERE id=%s", (self.source,))
                job = self.job()
                source = self.entities()[1]
                playlist = [
                    {"wall_time": source["actual_start"] + timedelta(seconds=point), "duration": 5}
                    for point in range(0, 1400, 5)
                ]
                if failure == "short_window":
                    playlist = playlist[-7:]
                elif failure == "gap":
                    del playlist[100]
                elif failure == "missing_anchor":
                    playlist[100]["wall_time"] = None
                media = Mock()
                media.manifest.return_value = playlist
                media.live_presentation_time.side_effect = lambda remote, item: (item["wall_time"] - source["actual_start"]).total_seconds()
                if failure in {"empty_frames", "403"}:
                    media.live_presentation_time.side_effect = WaitingSource(failure)
                processing = Processing(self.store, self.config, self.storage, youtube=Mock(), media=media)
                with self.assertRaises(WaitingSource):
                    processing.validate(job)
                self.assertEqual(self.store.one("SELECT state FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")["state"], "validating")
                self.assertFalse(self.store.one("SELECT seekable FROM pipeline.broadcasts WHERE id=%s", (self.broadcast,))["seekable"])
                self.assertEqual(self.entities()[1]["checkpoint_time"], 5850)
                self.store.finish(job)

    def test_live_clock_disagreement_or_discontinuity_cannot_publish(self):
        for failure in ["clock_disagreement", "discontinuity"]:
            with self.subTest(failure=failure):
                self.store.execute("UPDATE pipeline.broadcasts SET seekable=false WHERE id=%s", (self.broadcast,))
                job = self.job()
                source = self.entities()[1]
                media = Mock()
                media.manifest.return_value = [
                    {"wall_time": source["actual_start"] + timedelta(seconds=point), "duration": 5,
                     "group": int(failure == "discontinuity" and point >= 700)}
                    for point in range(0, 1400, 5)
                ]
                media.live_presentation_time.side_effect = [92.793, 700, 1307.793]
                processing = Processing(self.store, self.config, self.storage, youtube=Mock(), media=media)
                with self.assertRaises(NeedsReview):
                    processing.validate(job)
                self.assertEqual(self.store.one("SELECT state FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")["state"], "validating")
                self.assertEqual(self.entities()[0]["rounds"], series())
                self.store.finish(job)

    def test_old_seekability_metadata_requires_clock_verification(self):
        self.store.execute("UPDATE pipeline.sources SET metadata=%s,checkpoint_time=5850 WHERE id=%s",
                           (Jsonb({"live_dvr_range": {"start": 0, "end": 1400, "revision": 1}}), self.source))
        job = self.job()
        source = self.entities()[1]
        media = Mock()
        media.manifest.return_value = [
            {"wall_time": source["actual_start"] + timedelta(seconds=point), "duration": 5}
            for point in range(0, 1400, 5)
        ]
        media.live_presentation_time.side_effect = lambda remote, item: (item["wall_time"] - source["actual_start"]).total_seconds() + 7.793
        processing = Processing(self.store, self.config, self.storage, youtube=Mock(), media=media)
        processing.validate(job)
        saved = self.entities()[1]
        self.assertEqual(saved["checkpoint_time"], 5850)
        self.assertEqual(saved["detector_state"], source["detector_state"])
        self.assertEqual(media.live_presentation_time.call_count, 3)
        media.observation.assert_not_called()

    def test_archive_dependency_waits_survive_more_than_retry_budget(self):
        self.store.execute("UPDATE pipeline.broadcasts SET state='ended_waiting_archive' WHERE id=%s", (self.broadcast,))
        self.store.execute("UPDATE pipeline.sources SET checkpoint_time=5850 WHERE id=%s", (self.source,))
        self.store.enqueue("recover", "waiting-archive", broadcast=self.broadcast, source=self.source,
                           payload={"start": 100, "end": 200})
        worker = Worker(self.store, self.config, self.storage)
        for _ in range(15):
            self.assertTrue(worker.run_once())
            job = self.store.one("SELECT * FROM pipeline.jobs WHERE dedupe_key='waiting-archive'")
            self.assertEqual(job["state"], "waiting_source")
            self.assertEqual(job["failure_count"], 0)
            self.store.execute("UPDATE pipeline.jobs SET available_at=now() WHERE id=%s", (job["id"],))
        self.assertEqual(self.entities()[1]["checkpoint_time"], 5850)

    def test_closed_match_cannot_publish_over_unassigned_capture_gap(self):
        self.store.execute("UPDATE pipeline.segments SET evidence=%s WHERE id=%s", (Jsonb({"closed": True}), self.segment))
        self.store.enqueue("recover", "capture-gap", broadcast=self.broadcast, source=self.source,
                           payload={"start": 500, "end": 600})
        self.store.execute("UPDATE pipeline.jobs SET available_at=now()+interval '1 hour' WHERE dedupe_key='capture-gap'")
        job = self.job()
        processing = Processing(self.store, self.config, self.storage, media=Mock())
        with self.assertRaises(WaitingWork):
            processing.validate(job)
        self.assertEqual(self.store.one("SELECT state FROM pipeline.match_indexes")["state"], "validating")
        withheld = export_snapshot(self.store, self.storage)["withheld"]
        self.assertEqual(withheld[0]["expectedMatchId"], str(self.match))
        self.store.execute("UPDATE pipeline.jobs SET state='succeeded' WHERE dedupe_key='capture-gap'")
        processing.validate(job)
        self.assertEqual(self.store.one("SELECT state FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")["state"], "provisional")
        processing.media.window.assert_not_called()

    def test_live_dvr_recovery_resumes_without_replacing_capture_checkpoint(self):
        from detector import Observation
        import numpy as np

        self.store.execute("UPDATE pipeline.sources SET checkpoint_time=5850,detector_state=%s WHERE id=%s",
                           (Jsonb({"last_time": 5850, "version": "fixture"}), self.source))
        source = self.entities()[1]
        job = self.job("recover", payload={"start": 100, "end": 200})
        youtube, media = Mock(), Mock()
        media.manifest.return_value = [{"sequence": point // 5, "duration": 5,
                                        "wall_time": source["actual_start"] + timedelta(seconds=point)}
                                       for point in range(100, 200, 5)]
        frame = np.zeros((324, 1280, 3), dtype=np.uint8)
        media.live_frames.side_effect = lambda remote, item: [(offset, frame) for offset in range(5)]
        media.observation.side_effect = lambda frame, time, aliases: (Observation(time, 1, 100 - (time - 100), .99, scores=(0, 0)), {})
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        with self.assertRaises(ContinueJob):
            processing.recover(job)
        payload = self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"]
        self.assertEqual(payload["live_checkpoint"], 190)
        self.assertNotIn("checkpoint", payload)
        self.store.finish(job, "queued")
        self.store.execute("UPDATE pipeline.jobs SET priority=100 WHERE id=%s", (job["id"],))
        resumed = self.store.claim()
        Processing(Store(DATABASE), self.config, self.storage, youtube=youtube, media=media).recover(resumed)
        saved = self.entities()[1]
        self.assertEqual(saved["checkpoint_time"], source["checkpoint_time"])
        self.assertEqual(saved["detector_state"], source["detector_state"])
        self.assertEqual(media.live_frames.call_count, 20)
        self.assertEqual(len(self.store.rows("SELECT id FROM pipeline.round_candidates WHERE accepted")), 1)
        media.window.assert_not_called()

    def test_unavailable_live_recovery_range_keeps_checkpoint(self):
        job = self.job("recover", payload={"start": 100, "end": 200})
        source = self.entities()[1]
        media = Mock()
        media.manifest.return_value = [{"sequence": 50, "duration": 5,
                                       "wall_time": source["actual_start"] + timedelta(seconds=250)}]
        processing = Processing(self.store, self.config, self.storage, youtube=Mock(), media=media)
        with self.assertRaises(WaitingWork):
            processing.recover(job)
        self.assertEqual(self.entities()[1]["checkpoint_time"], source["checkpoint_time"])
        self.assertEqual(self.entities()[1]["detector_state"], source["detector_state"])
        media.live_frames.assert_not_called()

    def test_empty_live_recovery_does_not_mark_gap_covered(self):
        job = self.job("recover", payload={"start": 100, "end": 200})
        source = self.entities()[1]
        media = Mock()
        media.manifest.return_value = [{"sequence": 20, "duration": 5,
                                       "wall_time": source["actual_start"] + timedelta(seconds=100)}]
        media.live_frames.return_value = []
        processing = Processing(self.store, self.config, self.storage, youtube=Mock(), media=media)
        with self.assertRaises(WaitingSource):
            processing.recover(job)
        self.assertNotIn("live_checkpoint", self.store.one("SELECT payload FROM pipeline.jobs WHERE id=%s", (job["id"],))["payload"])
        self.assertEqual(self.entities()[1]["checkpoint_time"], source["checkpoint_time"])

    def test_repeated_publish_creates_no_duplicate_version(self):
        job = self.job()
        for _ in range(2):
            persist_index(self.store, job, *self.entities(), series(), "provisional", True)
        self.assertEqual(self.store.one("SELECT count(*) AS count FROM pipeline.match_indexes")["count"], 1)

    def test_reviewed_shadow_cutover_versions_identical_rounds(self):
        job = self.job()
        persist_index(self.store, job, *self.entities(), series(), "provisional", True)
        persist_index(self.store, job, *self.entities(), series(), "provisional", False)
        latest = self.store.one("SELECT * FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")
        self.assertEqual(latest["version"], 2)
        self.assertFalse(latest["shadow"])

    def test_better_recovery_can_promote_rejected_evidence_without_replacing_live_round(self):
        from detector import Observation

        job = self.job("recover")
        _, source, _ = self.entities()
        detector = CandidateDetector()
        detector.observe(Observation(100, 1, 100, 0.99))
        detector.observe(Observation(101, 1, 99, 0.99))
        accepted = detector.observe(Observation(102, 1, 98, 0.99))
        rejected = {**accepted, "accepted": False, "evidence": {"raw": "uncertain"}, "findings": ["unreadable_hud"]}
        self.store.checkpoint(job, source, 102, {}, [rejected], [], timeline="archive")
        self.store.checkpoint(job, source, 102, {}, [accepted], [], timeline="archive")
        promoted = self.store.one("SELECT * FROM pipeline.round_candidates")
        self.assertTrue(promoted["accepted"])
        self.assertEqual(promoted["evidence"]["prior_evidence"], {"raw": "uncertain"})
        self.store.checkpoint(job, source, 102, {}, [{**accepted, "evidence": {"start": 80}}], [], timeline="archive")
        self.assertEqual(self.store.one("SELECT * FROM pipeline.round_candidates")["evidence"]["start"], 100)

    def test_waiting_validation_is_not_starved_by_live_priority(self):
        self.store.enqueue("validate", "aged-validation", broadcast=self.broadcast)
        self.store.execute(
            "UPDATE pipeline.jobs SET available_at=now()-interval '10 minutes' WHERE dedupe_key='aged-validation'"
        )
        self.store.enqueue("live", "new-live", broadcast=self.broadcast, priority=10)
        self.assertEqual(self.store.claim()["kind"], "validate")

    def test_ready_match_validation_is_not_starved_by_archive_chunks(self):
        self.store.enqueue("reconcile", "archive-chunk", broadcast=self.broadcast, priority=80)
        self.store.enqueue("validate", "ready-match", broadcast=self.broadcast, priority=5)
        self.store.execute("UPDATE pipeline.jobs SET available_at=now()-interval '61 seconds' WHERE dedupe_key='ready-match'")
        self.assertEqual(self.store.claim()["dedupe_key"], "ready-match")

    def test_full_match_ownership_rejected_in_database(self):
        secondary = identifier()
        self.store.execute(
            "INSERT INTO pipeline.sources(id,broadcast_id,provider,external_id,role) VALUES (%s,%s,'youtube','editedvideo','full_match')",
            (secondary, self.broadcast),
        )
        with self.assertRaises(psycopg.errors.RaiseException):
            self.store.execute(
                """INSERT INTO pipeline.match_indexes(id,broadcast_id,expected_match_id,segment_id,canonical_source_id,generation,version,state,rounds,provenance)
                               VALUES (%s,%s,%s,%s,%s,1,1,'provisional',%s,'{}')""",
                (identifier(), self.broadcast, self.match, self.segment, secondary, Jsonb(series())),
            )

    def test_archive_readiness_failure_preserves_live_evidence(self):
        self.store.execute(
            "UPDATE pipeline.broadcasts SET state='ended_waiting_archive' WHERE id=%s", (self.broadcast,)
        )
        job = self.job("probe_archive")
        youtube = Mock()
        youtube.resolve.side_effect = WaitingSource("No video formats found")
        processing = Processing(self.store, self.config, self.storage, youtube=youtube)
        with self.assertRaises(WaitingSource):
            processing.probe_archive(job)
        self.store.finish(job, "waiting_source", "No video formats found", 30)
        self.assertEqual(self.store.one("SELECT * FROM pipeline.broadcasts")["state"], "ended_waiting_archive")
        self.assertEqual(self.store.one("SELECT * FROM pipeline.jobs")["state"], "waiting_source")
        self.assertEqual(self.store.one("SELECT count(*) AS count FROM pipeline.segments")["count"], 1)

    def test_duplicate_twitch_lifecycle_and_chat_events(self):
        channel = {"broadcaster_user_id": "8", "login": "fns", "official_youtube_channel_id": "official"}

        def envelope(kind, event, event_id):
            return {
                "metadata": {"message_id": event_id, "message_timestamp": "2026-10-04T10:02:00Z"},
                "payload": {"subscription": {"type": kind}, "event": {"broadcaster_user_id": "8", **event}},
            }

        online = envelope("stream.online", {"id": "123456789", "started_at": "2026-10-04T10:00:00Z"}, "online")
        for _ in range(2):
            ingest_event(self.store, online, [channel])
        chat = envelope(
            "channel.chat.message",
            {"message_id": "message-1", "chatter_user_name": "user", "message": {"text": "hello"}},
            "chat-1",
        )
        ingest_event(self.store, chat, [channel])
        ingest_event(self.store, {**chat, "metadata": {**chat["metadata"], "message_id": "chat-2"}}, [channel])
        self.assertEqual(
            self.store.one("SELECT count(*) AS count FROM pipeline.sources WHERE provider='twitch'")["count"], 1
        )
        self.assertEqual(self.store.one("SELECT count(*) AS count FROM pipeline.chat_messages")["count"], 1)
        self.assertEqual(self.store.one("SELECT * FROM pipeline.chat_messages")["media_time"], 120)

    def test_bounded_retry_is_per_job(self):
        first = self.job("probe_archive")
        self.store.execute("UPDATE pipeline.jobs SET max_attempts=1 WHERE id=%s", (first["id"],))
        first["max_attempts"] = 1
        self.store.finish(first, "retryable", "Transient media failure", 30)
        self.assertEqual(
            self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (first["id"],))["state"], "needs_review"
        )
        second = self.job("validate")
        self.assertIsNotNone(second)

    def test_piecewise_reconciliation_finalizes_without_ocr(self):
        self.store.execute(
            "UPDATE pipeline.sources SET metadata=%s WHERE id=%s",
            (Jsonb({"live_dvr_range": {"start": 0, "end": 100000, "revision": 1, "playback_shift": 7.793}}), self.source),
        )
        self.store.execute(
            "UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,)
        )
        job = self.job("reconcile")
        segment, source, _ = self.entities()
        archive = fingerprints(count=900, interval=2)
        live = {**archive, "frames": [{**frame, "time": frame["time"] + 100} for frame in archive["frames"][::5]]}
        self.store.checkpoint(job, source, 2000, {}, [], live["frames"])
        self.store.checkpoint(job, source, 2000, {}, [], archive["frames"], revision=1, timeline="archive")
        youtube = Mock()
        youtube.resolve.return_value = {"duration": 1800, "url": "fixture"}
        media = Mock()
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        processing.reconcile(job)
        media.window.assert_not_called()
        media.archive_fingerprints.assert_not_called()
        self.assertEqual(self.store.one("SELECT * FROM pipeline.broadcasts")["reconciled_revision"], 1)
        self.store.finish(job)
        final = self.store.claim()
        if final["kind"] == "segment":
            self.store.finish(final)
            final = self.store.claim()
        processing.finalize(final)
        self.assertEqual(self.store.one("SELECT * FROM pipeline.match_indexes")["state"], "final")
        self.assertEqual(self.store.one("SELECT * FROM pipeline.match_indexes")["rounds"][0]["start"], 0)

    def test_youtube_403_retries_without_modifying_archive_checkpoint(self):
        self.store.enqueue("recover", "archive-403", broadcast=self.broadcast, source=self.source, payload={"checkpoint": 5850, "detector_state": {"previous": {"round": 5, "start": 5831}, "map_number": 1}})
        processing = Mock()
        processing.recover.side_effect = WaitingSource("FFmpeg media read failed: Server returned 403 Forbidden")
        worker = Worker(self.store, self.config, self.storage, coordinator=Mock(), processing=processing)
        worker.run_once()
        saved = self.store.one("SELECT * FROM pipeline.jobs WHERE dedupe_key='archive-403'")
        processing.youtube.reject.assert_called_once_with("abcdefghijk")
        self.assertEqual(saved["state"], "waiting_source")
        self.assertEqual(saved["payload"]["checkpoint"], 5850)
        self.assertEqual(saved["payload"]["detector_state"]["previous"]["round"], 5)
        self.assertEqual(saved["failure_count"], 1)
        self.store.execute("UPDATE pipeline.jobs SET available_at=now() WHERE id=%s", (saved["id"],))
        restart = Worker(Store(DATABASE), self.config, self.storage, coordinator=Mock(), processing=Mock())
        restart.run_once()
        resumed = self.store.one("SELECT * FROM pipeline.jobs WHERE id=%s", (saved["id"],))
        self.assertEqual(resumed["state"], "succeeded")
        self.assertEqual(resumed["payload"], saved["payload"])

    def test_worker_dispatch_retry_and_restart(self):
        self.store.enqueue("probe_archive", "worker-restart", broadcast=self.broadcast, source=self.source)
        processing = Mock()
        processing.probe_archive.side_effect = WaitingSource("No video formats found")
        first = Worker(self.store, self.config, self.storage, coordinator=Mock(), processing=processing)
        first.run_once()
        job = self.store.one("SELECT * FROM pipeline.jobs")
        self.assertEqual(job["state"], "waiting_source")
        self.store.execute("UPDATE pipeline.jobs SET available_at=now() WHERE id=%s", (job["id"],))
        second = Worker(Store(DATABASE), self.config, self.storage, coordinator=Mock(), processing=Mock())
        second.run_once()
        self.assertEqual(self.store.one("SELECT * FROM pipeline.jobs")["state"], "succeeded")
        self.assertEqual(len(self.store.rows("SELECT * FROM pipeline.attempts")), 2)

    def test_schedule_failure_does_not_delete_durable_matches(self):
        provider = Mock()
        provider.matches.side_effect = OSError("Provider down")
        ingest_schedule(self.store, [provider])
        self.assertEqual(self.store.one("SELECT count(*) AS count FROM pipeline.expected_matches")["count"], 1)

    def test_expected_broadcast_candidates_segment_provisional_and_final_flow(self):
        from detector import Observation

        youtube = Mock()
        youtube.metadata.return_value = {
            "state": "live",
            "actual_start": timestamp("2026-10-04T10:00:00Z"),
            "actual_end": None,
            "metadata": {"liveStreamingDetails": {"actualStartTime": "2026-10-04T10:00:00Z"}},
        }
        job = self.job(
            "associate",
            payload={
                "entry": {"id": "abcdefghijk", "title": "Champions broadcast"},
                "channel": {"channel_id": "official", "region": "international", "timezone": "UTC"},
            },
        )
        Coordinator(self.store, self.config, youtube).associate(job)
        self.store.finish(job)
        self.store.execute("UPDATE pipeline.jobs SET state='succeeded',lease_token=NULL,lease_until=NULL")
        capture = self.job("live")
        _, source, _ = self.entities()
        detector = CandidateDetector()
        candidates = []
        for item in series():
            for second in range(3):
                candidate = detector.observe(
                    Observation(
                        item["start"] + second, item["round"], 100 - second, 0.99, scores=tuple(item["scores"])
                    ),
                    extra={"teams": ["A", "B"]},
                )
                candidates.append(candidate)
        self.store.checkpoint(
            capture, source, 1302, detector.state(), candidates, fingerprints(count=750, interval=2)["frames"][::5]
        )
        self.store.finish(capture)
        segment_job = self.job("segment")
        processing = Processing(self.store, self.config, self.storage)
        processing.segment(segment_job)
        self.store.finish(segment_job)
        validation = self.store.claim()
        processing.validate(validation)
        self.store.finish(validation)
        provisional = self.store.one("SELECT * FROM pipeline.match_indexes")
        self.assertEqual(provisional["state"], "provisional")
        self.store.execute(
            "UPDATE pipeline.broadcasts SET state='archive_ready',archive_revision=1 WHERE id=%s", (self.broadcast,)
        )
        reconciliation = self.job("reconcile")
        _, source, _ = self.entities()
        self.store.checkpoint(
            reconciliation,
            source,
            1302,
            detector.state(),
            [],
            fingerprints(count=750, interval=2)["frames"],
            revision=1,
            timeline="archive",
        )
        youtube.resolve.return_value = {"duration": 1500, "url": "fixture"}
        processing.youtube = youtube
        processing.reconcile(reconciliation)
        self.store.finish(reconciliation)
        self.store.execute("UPDATE pipeline.jobs SET state='succeeded' WHERE kind='segment'")
        final = self.store.claim()
        processing.finalize(final)
        latest = self.store.one("SELECT * FROM pipeline.match_indexes ORDER BY version DESC LIMIT 1")
        self.assertEqual(latest["state"], "final")
        self.assertEqual(latest["canonical_source_id"], self.source)
        self.assertEqual(len(latest["rounds"]), 13)

    def test_refreshed_live_url_resumes_idempotently(self):
        from detector import Observation
        import numpy as np

        job = self.job("live")
        youtube = Mock()
        youtube.resolve.side_effect = [{"url": "expired"}, {"url": "refreshed"}]
        media = Mock()
        media.scan_frames.side_effect = lambda *args, **kwargs: MediaAnalysis.scan_frames(media, *args, **kwargs)
        media.manifest.return_value = [{"wall_time": timestamp("2026-10-04T10:00:00Z"), "duration": 6}]
        media.live_frames.side_effect = [
            WaitingSource("Expired media URL"),
            [(offset, np.zeros((324, 1280, 3), dtype=np.uint8)) for offset in range(6)],
        ]
        media.observation.side_effect = lambda frame, time, *args, **kwargs: (
            Observation(time, 1, 100 - int(time), 0.99, scores=(0, 0)),
            {"teams": ["A", "B"]},
        )
        media.fingerprint.return_value = {"hash": "ff" * 8}
        processing = Processing(self.store, self.config, self.storage, youtube=youtube, media=media)
        with self.assertRaises(WaitingSource):
            processing.live(job)
        processing.live(job)
        self.assertEqual(youtube.resolve.call_count, 2)
        self.assertEqual(
            self.store.one("SELECT checkpoint_time FROM pipeline.sources WHERE id=%s", (self.source,))[
                "checkpoint_time"
            ],
            5,
        )
        self.assertEqual(
            self.store.one("SELECT count(*) AS count FROM pipeline.round_candidates WHERE accepted")["count"], 1
        )
        media.manifest.assert_called_with({"url": "refreshed"}, start_sequence=0)

    def test_live_resume_uses_durable_dvr_sequence_and_skips_saved_frames(self):
        import numpy as np
        from detector import Observation

        self.store.execute("UPDATE pipeline.sources SET checkpoint_time=105,detector_state=%s WHERE id=%s",
                           (Jsonb({"media_sequence": 20}), self.source))
        job = self.job("live")
        media = Mock()
        media.scan_frames.side_effect = lambda *args, **kwargs: MediaAnalysis.scan_frames(media, *args, **kwargs)
        media.manifest.return_value = [
            {"wall_time": timestamp("2026-10-04T10:00:00Z") + timedelta(seconds=100), "duration": 5, "sequence": 20},
            {"wall_time": timestamp("2026-10-04T10:00:00Z") + timedelta(seconds=105), "duration": 5, "sequence": 21},
        ]
        media.live_frames.return_value = [(offset, np.zeros((324, 1280, 3), dtype=np.uint8)) for offset in range(5)]
        media.observation.side_effect = lambda frame, time, *args, **kwargs: (Observation(time, None, None), {})
        media.fingerprint.return_value = {"hash": "ff" * 8}
        youtube = Mock()
        youtube.resolve.return_value = {"url": "refreshed"}
        Processing(Store(DATABASE), self.config, self.storage, youtube=youtube, media=media).live(job)
        media.manifest.assert_called_once_with({"url": "refreshed"}, start_sequence=20)
        self.assertEqual(media.live_frames.call_count, 1)
        source = self.entities()[1]
        self.assertEqual(source["checkpoint_time"], 109)
        self.assertEqual(source["detector_state"]["media_sequence"], 21)


if __name__ == "__main__":
    unittest.main()
