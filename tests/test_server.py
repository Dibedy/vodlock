import http.client
import json
import sys
import threading
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "indexer"))
import server


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def request(self, method, path, body=None, **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port)
        values = {"Host": "127.0.0.1:8766", **headers}
        connection.request(method, path, body, values)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_video_link_validation(self):
        self.assertEqual(server.video_id("https://www.youtube.com/watch?v=ZphbktbT26k&t=3760s"), "ZphbktbT26k")
        self.assertEqual(server.video_id("https://youtu.be/ZphbktbT26k"), "ZphbktbT26k")
        for link in ["http://www.youtube.com/watch?v=ZphbktbT26k", "https://youtube.com.evil.test/watch?v=ZphbktbT26k", "https://www.youtube.com/@channel", "https://youtu.be/short"]:
            with self.assertRaises(ValueError):
                server.video_id(link)

    def test_analysis_copy_limit_allows_long_twitch_archives(self):
        self.assertEqual(server.ANALYSIS_MAX_BYTES, 8 * 1024 ** 3)

    def test_analysis_copy_limit_preserves_free_disk_and_honors_hosted_cap(self):
        usage = SimpleNamespace(free=10 * 1024 ** 3)
        with patch.object(server.shutil, "disk_usage", return_value=usage), \
                patch.dict(server.os.environ, {"VODLOCK_ANALYSIS_MAX_GB": "5"}):
            self.assertEqual(server.analysis_download_limit(Path(".")), 5 * 1024 ** 3)
        usage = SimpleNamespace(free=4 * 1024 ** 3)
        with patch.object(server.shutil, "disk_usage", return_value=usage), patch.dict(server.os.environ, {}, clear=True):
            self.assertEqual(server.analysis_download_limit(Path(".")), 2 * 1024 ** 3)

    def test_analysis_copy_refuses_to_consume_disk_reserve(self):
        usage = SimpleNamespace(free=server.ANALYSIS_RESERVE_BYTES + server.MINIMUM_ANALYSIS_BYTES - 1)
        with patch.object(server.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(server.AnalysisSizeLimitError):
                server.analysis_download_limit(Path("."))

    def test_analysis_keeps_dense_opening_samples_and_coarsens_the_rest(self):
        self.assertEqual(server.analysis_sample_time(0), 0)
        self.assertEqual(server.analysis_sample_time(server.INITIAL_DENSE_SAMPLE_SECONDS - 1),
                         server.INITIAL_DENSE_SAMPLE_SECONDS - 1)
        self.assertEqual(server.analysis_sample_time(server.INITIAL_DENSE_SAMPLE_SECONDS),
                         server.INITIAL_DENSE_SAMPLE_SECONDS)
        self.assertEqual(server.analysis_sample_time(server.INITIAL_DENSE_SAMPLE_SECONDS + 1),
                         server.INITIAL_DENSE_SAMPLE_SECONDS + server.COARSE_SAMPLE_INTERVAL)
        self.assertIn("select='lt(n\\,900)+gte(n\\,900)*not(mod(n\\,2))'", server.analysis_video_filter())

    def test_twitch_vod_link_validation(self):
        self.assertEqual(server.twitch_video_id("https://www.twitch.tv/videos/1234567890"), "1234567890")
        for link in ["https://www.twitch.tv/gofns", "https://twitch.tv/directory", "http://twitch.tv/videos/1234567890"]:
            with self.assertRaises(ValueError):
                server.twitch_video_id(link)

    def test_remote_download_retries_at_lower_resolution(self):
        identifier = "a" * 32
        job = {"id": identifier, "videoId": "ZphbktbT26k", "status": "downloading", "progress": 10}
        attempts = []

        class DownloadError(Exception):
            pass

        class YoutubeDL:
            def __init__(self, options):
                self.options = dict(options)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def extract_info(self, _, download):
                attempts.append(self.options["format"])
                source = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
                if len(attempts) == 1:
                    source.with_suffix(".mp4.part").write_bytes(b"partial")
                    raise DownloadError("HTTP Error 403: Forbidden")
                source.write_bytes(b"video")
                return {"path": str(source)}

            def prepare_filename(self, info):
                return info["path"]

        yt_dlp = SimpleNamespace(YoutubeDL=YoutubeDL, utils=SimpleNamespace(DownloadError=DownloadError))
        with tempfile.TemporaryDirectory() as temporary, patch.object(server, "DATA", Path(temporary)), patch.dict(server.JOBS, {identifier: job}):
            work = Path(temporary) / identifier
            work.mkdir()
            source = server.download_youtube(job, work, lambda _: None, yt_dlp)
            self.assertEqual(source.read_bytes(), b"video")
            self.assertEqual(len(attempts), 2)
            self.assertIn("height<=540", attempts[1])
            self.assertFalse((work / "source.mp4.part").exists())
            self.assertIn("trying another", job["message"])

    def test_remote_stream_resolution_does_not_download_the_archive(self):
        calls = []

        class DownloadError(Exception):
            pass

        class YoutubeDL:
            def __init__(self, options):
                self.options = dict(options)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def extract_info(self, url, download):
                calls.append((url, download, self.options["format"]))
                return {"url": "https://media.example/video.m3u8", "duration": 7200,
                        "http_headers": {"User-Agent": "test"}}

        yt_dlp = SimpleNamespace(YoutubeDL=YoutubeDL, utils=SimpleNamespace(DownloadError=DownloadError))
        result = server.resolve_remote({"kind": "twitch", "twitchVideoId": "1234567890"}, yt_dlp)
        self.assertEqual(result["url"], "https://media.example/video.m3u8")
        self.assertEqual(result["duration"], 7200)
        self.assertEqual(calls[0][1], False)

    def test_remote_stream_uses_ytdlp_stdout_instead_of_the_media_url(self):
        job = {"kind": "twitch", "twitchVideoId": "1234567890"}
        command = server.stream_command(job, {"format": "bestvideo[height<=720]", "playerClient": None})
        self.assertIn("yt_dlp", command)
        self.assertIn("--output", command)
        self.assertEqual(command[command.index("--output") + 1], "-")
        self.assertEqual(command[-1], "https://www.twitch.tv/videos/1234567890")

    def test_youtube_hosted_worker_tries_supported_player_clients(self):
        identifier = "e" * 32
        job = {"id": identifier, "kind": "youtube", "videoId": "ZphbktbT26k", "status": "downloading", "progress": 0}
        clients = []

        class DownloadError(Exception):
            pass

        class YoutubeDL:
            def __init__(self, options):
                clients.append(options.get("extractor_args", {}).get("youtube", {}).get("player_client", [None])[0])

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def extract_info(self, *_args, **_kwargs):
                raise DownloadError("blocked")

        yt_dlp = SimpleNamespace(YoutubeDL=YoutubeDL, utils=SimpleNamespace(DownloadError=DownloadError))
        with tempfile.TemporaryDirectory() as temporary, patch.object(server, "DATA", Path(temporary)), \
                patch.dict(server.JOBS, {identifier: job}), patch.dict(server.os.environ, {"VODLOCK_YOUTUBE_POT": "1"}):
            work = Path(temporary) / identifier
            work.mkdir()
            with self.assertRaises(DownloadError):
                server.download_remote(job, work, lambda _: None, yt_dlp)
        self.assertEqual(clients, [None, "mweb", "web_safari", "web_embedded"])

    def test_size_filtered_download_tries_next_format(self):
        identifier = "f" * 32
        job = {"id": identifier, "kind": "twitch", "twitchVideoId": "1234567890", "status": "downloading"}
        attempts = []

        class DownloadError(Exception):
            pass

        class YoutubeDL:
            def __init__(self, options):
                self.options = dict(options)
                attempts.append(self.options["format"])

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def extract_info(self, *_args, **_kwargs):
                path = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
                if len(attempts) > 1:
                    path.write_bytes(b"video")
                return {"path": str(path)}

            def prepare_filename(self, info):
                return info["path"]

        yt_dlp = SimpleNamespace(YoutubeDL=YoutubeDL, utils=SimpleNamespace(DownloadError=DownloadError))
        with tempfile.TemporaryDirectory() as temporary, patch.object(server, "DATA", Path(temporary)), \
                patch.dict(server.JOBS, {identifier: job}):
            work = Path(temporary) / identifier
            work.mkdir()
            source = server.download_remote(job, work, lambda _: None, yt_dlp)
            self.assertEqual(source.read_bytes(), b"video")
        self.assertEqual(len(attempts), 2)
        self.assertIn("fps<=30", attempts[0])
        self.assertIn("height=720", attempts[0])

    def test_dns_rebinding_host_rejected(self):
        status, _ = self.request("GET", "/api/state", Host="evil.test:8766")
        self.assertEqual(status, 403)

    def test_write_requires_token(self):
        status, _ = self.request("POST", "/api/jobs", "{}")
        self.assertEqual(status, 403)

    def test_cross_origin_write_rejected(self):
        status, _ = self.request("POST", "/api/jobs", "{}", **{"X-VODLOCK-Token": server.TOKEN, "Origin": "https://evil.test"})
        self.assertEqual(status, 403)

    def test_invalid_json_and_non_object_requests_rejected(self):
        for body in ["[1]", "null", "invalid", "{}"]:
            status, _ = self.request("POST", "/api/jobs", body, **{"X-VODLOCK-Token": server.TOKEN})
            self.assertEqual(status, 400)

    def test_missing_export_and_unknown_path(self):
        for path in ["/api/export/" + "a" * 32, "/../../server.py"]:
            status, _ = self.request("GET", path)
            self.assertEqual(status, 404)

    def test_review_validates_rounded_timestamps_and_persists_correction(self):
        identifier = "b" * 32
        job = {"id": identifier, "label": "Test", "status": "ready", "duration": 500,
               "rounds": [{"map": 1, "round": 1, "start": 100}, {"map": 1, "round": 2, "start": 250}]}
        with tempfile.TemporaryDirectory() as temporary, patch.object(server, "DATA", Path(temporary)), patch.dict(server.JOBS, {identifier: job}):
            headers = {"X-VODLOCK-Token": server.TOKEN}
            for start in [100.001, 500, -1, True, "120", float("nan")]:
                status, _ = self.request("POST", "/api/review/" + identifier, json.dumps({"index": 1, "map": 1, "round": 2, "start": start}), **headers)
                self.assertEqual(status, 400)
            status, _ = self.request("POST", "/api/review/" + identifier, json.dumps({"index": 1, "map": 1, "round": 2, "start": 260.5}), **headers)
            self.assertEqual(status, 200)
            persisted = json.loads((Path(temporary) / (identifier + ".json")).read_text())
            self.assertEqual(persisted["rounds"][1]["start"], 260.5)
            self.assertTrue(persisted["rounds"][1]["verified"])

    def test_manual_rounds_and_reversible_exclusion(self):
        identifier = "c" * 32
        job = {"id": identifier, "label": "Test", "status": "ready", "duration": 500,
               "rounds": [{"map": 1, "round": 1, "start": 100}, {"map": 1, "round": 3, "start": 350}]}
        with tempfile.TemporaryDirectory() as temporary, patch.object(server, "DATA", Path(temporary)), patch.dict(server.JOBS, {identifier: job}):
            headers = {"X-VODLOCK-Token": server.TOKEN}
            for start in [100, 400, True, -10]:
                status, _ = self.request("POST", "/api/review/" + identifier, json.dumps({"action": "add", "map": 1, "round": 2, "start": start}), **headers)
                self.assertEqual(status, 400)
                self.assertEqual(len(job["rounds"]), 2)
            status, _ = self.request("POST", "/api/review/" + identifier, json.dumps({"action": "add", "map": 1, "round": 2, "start": 200}), **headers)
            self.assertEqual(status, 200)
            self.assertEqual([entry["round"] for entry in job["rounds"]], [1, 2, 3])
            self.assertEqual(job["warnings"], [])
            status, _ = self.request("POST", "/api/review/" + identifier, json.dumps({"index": 1, "map": 1, "round": 3, "start": 300}), **headers)
            self.assertEqual(status, 400)
            self.assertEqual(job["rounds"][1]["start"], 200)
            for action, length in [("exclude", 2), ("include", 3)]:
                status, _ = self.request("POST", "/api/review/" + identifier, json.dumps({"action": action, "index": 1, "map": 1, "round": 2}), **headers)
                self.assertEqual(status, 200)
                self.assertEqual(len(server.export(job)["rounds"]), length)
                self.assertEqual(len(job["rounds"]), 3)

    def test_save_failure_does_not_change_review_state(self):
        identifier = "d" * 32
        job = {"id": identifier, "status": "ready", "duration": 500, "rounds": [{"map": 1, "round": 1, "start": 100}]}
        with patch.dict(server.JOBS, {identifier: job}), patch.object(server, "save", side_effect=OSError("Disk full")):
            status, value = self.request("POST", "/api/review/" + identifier, json.dumps({"index": 0, "map": 1, "round": 1, "start": 200}), **{"X-VODLOCK-Token": server.TOKEN})
            self.assertEqual(status, 500)
            self.assertIn("Disk full", value["error"])
            self.assertEqual(job["rounds"][0]["start"], 100)

    def test_shutdown_requires_token_and_uses_server_lifecycle(self):
        completed = threading.Event()
        with patch.object(self.http, "shutdown", side_effect=completed.set), patch.object(server, "CANCEL") as cancellation:
            status, _ = self.request("POST", "/api/shutdown", "{}")
            self.assertEqual(status, 403)
            self.assertFalse(completed.is_set())
            status, _ = self.request("POST", "/api/shutdown", "{}", **{"X-VODLOCK-Token": server.TOKEN})
            self.assertEqual(status, 200)
            self.assertTrue(completed.wait(1))
            cancellation.set.assert_called_once()


if __name__ == "__main__":
    unittest.main()
