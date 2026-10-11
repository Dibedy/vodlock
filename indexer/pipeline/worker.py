import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Config
from .coordinator import Coordinator
from .deployment import Deployment
from .errors import ContinueJob, NeedsReview, StaleAttempt, Unsupported, WaitingSource, WaitingWork, failure_kind, retry_after
from .processing import Processing
from .publishing import export_snapshot
from .storage import configured_storage
from .store import Store, backoff
from .twitch import TwitchCapture, eventsub_loop


LOG = logging.getLogger(__name__)


class Worker:
    def __init__(self, store, config, storage=None, coordinator=None, processing=None):
        self.store = store
        self.config = config
        self.storage = storage or configured_storage(config)
        self.coordinator = coordinator or Coordinator(store, config)
        self.processing = processing or Processing(store, config, self.storage)
        self.twitch = TwitchCapture(store, config, self.processing)
        self.deployment = Deployment(store, config)
        self.stop = threading.Event()
        self.processing.stop = self.stop
        self.last_tick = time.monotonic()
        self.handlers = {
            "discover_youtube": self.coordinator.discover_youtube,
            "associate": self.coordinator.associate,
            "associate_upload": self.coordinator.associate_upload,
            "refresh": self.coordinator.refresh,
            "live": self.processing.live,
            "probe_archive": self.processing.probe_archive,
            "segment": self.processing.segment,
            "validate": self.processing.validate,
            "reconcile": self.processing.reconcile,
            "fingerprint_archive": self.processing.fingerprint_archive,
            "reconcile_segment": self.processing.reconcile_segment,
            "finalize": self.processing.finalize,
            "recover": self.processing.recover,
            "validate_upload": self.processing.validate_upload,
            "twitch_live": self.twitch.live,
            "twitch_vod": self.twitch.vod,
            "twitch_event": self.twitch.event,
            "twitch_align": self.twitch.align,
            "chat_gaps": self.twitch.chat_gaps,
            "export": self.export,
            "deploy": self.deployment.publish,
            "verify_deployment": self.deployment.verify,
            "discover_twitch_vods": self.twitch.discover_vods,
            "associate_twitch_vod": self.twitch.associate_vod,
        }

    def export(self, job):
        for shadow in [True, False] if not self.config.shadow else [True]:
            export_snapshot(self.store, self.storage, shadow, None if shadow else self.config.approved_ids(self.store))
        if not self.config.shadow and (self.config.settings or {}).get("deployment"):
            self.store.enqueue("deploy", "deploy:site", priority=50)

    def lease_loop(self, job, done):
        while not done.wait(max(1, self.config.lease_seconds // 3)):
            try:
                self.store.heartbeat(job, self.config.lease_seconds)
            except Exception:
                LOG.exception("lease_renewal_failed job_id=%s", job["id"])
                return

    def queue_export(self, job, previous_index):
        if job["kind"] in {"validate", "finalize", "reconcile_segment"}:
            current = self.store.one("""SELECT i.id,i.segment_revision,s.revision FROM pipeline.match_indexes i
                JOIN pipeline.segments s ON s.id=i.segment_id WHERE i.expected_match_id=%s AND i.generation=%s
                ORDER BY i.version DESC LIMIT 1""", (job["expected_match_id"], job["generation"]))
            if previous_index == ({"id": current["id"]} if current else None) and (not current or current["segment_revision"] == current["revision"]):
                return
        elif job["kind"] != "validate_upload":
            return
        self.store.enqueue("export", f"export:{job['id']}", broadcast=job["broadcast_id"])
        if job["kind"] in {"validate", "finalize", "reconcile_segment"}:
            self.store.execute("""UPDATE pipeline.jobs SET available_at=now(),wait_count=0 WHERE dedupe_key='deploy:site'
                AND state='waiting_source' AND last_error='Website publication is batching incremental chat and alignment updates'""")

    def run_once(self):
        self.last_tick = time.monotonic()
        job = self.store.claim(self.config.lease_seconds)
        if not job:
            return False
        done = threading.Event()
        heartbeat = threading.Thread(target=self.lease_loop, args=(job, done), daemon=True)
        heartbeat.start()
        previous_index = None
        try:
            if job["kind"] in {"validate", "finalize", "reconcile_segment"}:
                previous_index = self.store.one("SELECT id FROM pipeline.match_indexes WHERE expected_match_id=%s AND generation=%s ORDER BY version DESC LIMIT 1", (job["expected_match_id"], job["generation"]))
            handler = self.handlers.get(job["kind"])
            if handler is None:
                raise Unsupported("Unknown job kind: " + job["kind"])
            handler(job)
            self.queue_export(job, previous_index)
            self.store.finish(job)
            if job["kind"] in {"live", "twitch_live"}:
                live = (
                    self.store.one("SELECT state FROM pipeline.broadcasts WHERE id=%s", (job["broadcast_id"],))
                    if job["kind"] == "live"
                    else self.store.one("SELECT state FROM pipeline.sources WHERE id=%s", (job["source_id"],))
                )
                if live and live["state"] == "live":
                    self.store.enqueue(
                        job["kind"],
                        job["dedupe_key"],
                        broadcast=job["broadcast_id"],
                        source=job["source_id"],
                        payload=job["payload"],
                        priority=job["priority"],
                        delay=3,
                    )
        except StaleAttempt as error:
            LOG.warning("stale_attempt_rejected job_id=%s reason=%s", job["id"], error)
        except ContinueJob as progress:
            try:
                self.store.finish(job, "queued", delay=0 if job["kind"] in {"reconcile", "fingerprint_archive", "recover"} else 1)
                LOG.info("job_checkpointed job_id=%s reason=%s", job["id"], progress)
            except StaleAttempt:
                LOG.warning("checkpointed_attempt_already_stale job_id=%s", job["id"])
        except InterruptedError:
            try:
                self.store.finish(job, "queued", delay=1, failure_kind="interrupted")
            except StaleAttempt:
                LOG.warning("interrupted_attempt_already_stale job_id=%s", job["id"])
        except Exception as error:
            kind = failure_kind(error)
            if isinstance(error, WaitingSource) and not isinstance(error, WaitingWork):
                if "403" in str(error) and job.get("source_id"):
                    source = self.store.one("SELECT provider,external_id FROM pipeline.sources WHERE id=%s", (job["source_id"],))
                    if source and source["provider"] == "youtube":
                        self.processing.youtube.reject(source["external_id"])
                    else:
                        self.processing.youtube.invalidate()
                else:
                    self.processing.youtube.invalidate()
            state = (
                "needs_review"
                if kind == "configuration"
                else
                "waiting_source"
                if isinstance(error, WaitingSource)
                else "needs_review"
                if isinstance(error, NeedsReview)
                else "unsupported"
                if isinstance(error, Unsupported)
                else "retryable"
            )
            log = LOG.info if isinstance(error, WaitingWork) else LOG.warning if isinstance(error, (WaitingSource, NeedsReview, Unsupported)) else LOG.exception
            if isinstance(error, WaitingWork) and job.get("last_error") == str(error):
                log = LOG.debug
            log(
                ("job_waiting" if isinstance(error, WaitingWork) else "job_failed") + " job_id=%s broadcast_id=%s source_id=%s expected_match_id=%s state=%s reason=%s",
                job["id"],
                job["broadcast_id"],
                job["source_id"],
                job["expected_match_id"],
                state,
                error,
            )
            LOG.debug("job_failure_evidence job_id=%s", job["id"], exc_info=True)
            try:
                delay = max(60, backoff(job.get("wait_count", 0), jitter=True)) if isinstance(error, WaitingWork) else backoff(job["failure_count"], jitter=True)
                requested = retry_after(error)
                if requested is not None:
                    delay = max(delay, min(86400, requested))
                self.store.finish(job, state, str(error), delay, dependency=kind == "dependency", failure_kind=kind)
                LOG.info("job_failure_classified job_id=%s failure_kind=%s retry_delay_seconds=%.3f", job["id"], kind, delay)
                self.queue_export(job, previous_index)
            except StaleAttempt:
                LOG.warning("failed_attempt_already_stale job_id=%s", job["id"])
        finally:
            done.set()
            heartbeat.join(timeout=5)
            self.last_tick = time.monotonic()
        return True

    def discovery_loop(self):
        while not self.stop.is_set():
            try:
                self.coordinator.discover()
            except Exception:
                LOG.exception("coordinator_tick_failed")
            self.stop.wait(self.config.discovery_seconds)

    def processing_loop(self):
        while not self.stop.is_set():
            try:
                if not self.run_once():
                    self.stop.wait(self.config.poll_seconds)
            except Exception:
                LOG.exception("worker_tick_failed")
                self.stop.wait(self.config.poll_seconds)

    def run(self):
        from yt_dlp.globals import all_plugins_loaded
        from yt_dlp.plugins import load_all_plugins

        if not all_plugins_loaded.value:
            load_all_plugins()
        self.store.migrate()
        threads = [
            threading.Thread(target=self.discovery_loop, daemon=True),
            threading.Thread(target=eventsub_loop, args=(self.store, self.config, self.stop), daemon=True),
        ]
        processors = []
        for number in range(1, self.config.processing_slots):
            worker = Worker(self.store, self.config, self.storage, coordinator=self.coordinator)
            worker.processing.media.stream_cache = self.processing.media.stream_cache
            worker.processing.media.stream_lock = self.processing.media.stream_lock
            worker.processing.media.video_cache = self.processing.media.video_cache
            worker.processing.media.video_lock = self.processing.media.video_lock
            worker.twitch.media.stream_cache = self.twitch.media.stream_cache
            worker.twitch.media.stream_lock = self.twitch.media.stream_lock
            worker.stop = self.stop
            worker.processing.stop = self.stop
            processors.append(threading.Thread(target=worker.processing_loop, name=f"processing-{number + 1}"))
        LOG.info("worker_started processing_slots=%s", self.config.processing_slots)
        for thread in threads + processors:
            thread.start()
        try:
            self.processing_loop()
        finally:
            self.stop.set()
            for thread in processors:
                thread.join()
            self.processing.media.close()
            self.twitch.media.close()


def status_server(worker, host, port):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            token = os.environ.get("SPOILLESS_STATUS_TOKEN")
            return bool(token and self.headers.get("Authorization") == "Bearer " + token)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self.authorized() or (origin and origin != "http://" + self.headers.get("Host", "")):
                self.respond(401, b'{"error":"Unauthorized"}')
            elif urlparse(self.path).path == "/shutdown":
                if os.environ.get("SPOILLESS_SUPERVISOR_STOP"):
                    Path(os.environ["SPOILLESS_SUPERVISOR_STOP"]).touch()
                worker.stop.set()
                self.respond(200, b'{"state":"stopping"}')
            elif urlparse(self.path).path == "/retry-job":
                import uuid

                if worker.stop.is_set():
                    self.respond(409, b'{"error":"Worker is stopping. Restart it before retrying tasks."}')
                    return
                try:
                    job_id = uuid.UUID(parse_qs(urlparse(self.path).query).get("id", [""])[0])
                except ValueError:
                    self.respond(400, b'{"error":"Invalid task ID"}')
                    return
                try:
                    worker.store.retry_job(job_id, review_only=True)
                    self.respond(200, b'{"state":"queued"}')
                except ValueError:
                    self.respond(409, b'{"error":"Task is no longer eligible for review. Refresh its status."}')
                except Exception:
                    LOG.exception("operator_retry_failed")
                    self.respond(503, b'{"error":"Database unavailable. Retry was not confirmed; refresh task status."}')
            else:
                self.respond(404, b'{"error":"Not found"}')

        def do_GET(self):
            parsed = urlparse(self.path)
            status = 200
            assets = {"/": ("status.html", "text/html; charset=utf-8"),
                      "/status.css": ("status.css", "text/css; charset=utf-8"),
                      "/status.js": ("status.js", "text/javascript; charset=utf-8")}
            if parsed.path in assets:
                name, content_type = assets[parsed.path]
                self.respond(200, (Path(__file__).parent / "web" / name).read_bytes(), content_type)
                return
            if parsed.path == "/health":
                try:
                    worker.store.one("SELECT 1 AS ready")
                    value = {"state": "stopping" if worker.stop.is_set() else "ready",
                             "mode": "shadow" if worker.config.shadow else "publish",
                             "workspace": str(Path(__file__).resolve().parents[2]), "pid": os.getpid(),
                             "parent_pid": os.getppid()}
                    if os.environ.get("SPOILLESS_SUPERVISOR_PID"):
                        value["supervisor_pid"] = int(os.environ["SPOILLESS_SUPERVISOR_PID"])
                        value["supervisor_parent_pid"] = int(os.environ.get("SPOILLESS_SUPERVISOR_PARENT_PID", "0"))
                except Exception:
                    status, value = 503, {"state": "database_unavailable"}
            elif parsed.path in {"/status", "/operator-status"}:
                if not self.authorized():
                    status, value = 401, {"error": "Unauthorized"}
                else:
                    import uuid

                    broadcast = parse_qs(parsed.query).get("broadcast", [None])[0]
                    try:
                        if parsed.path == "/operator-status":
                            value = worker.store.operator_status(worker.config)
                            value["worker"] = {"state": "stopping" if worker.stop.is_set() else "running",
                                               "mode": "shadow" if worker.config.shadow else "publish"}
                        else:
                            value = worker.store.status(uuid.UUID(broadcast) if broadcast else None)
                    except ValueError:
                        status, value = 400, {"error": "Invalid broadcast ID"}
                    except Exception:
                        LOG.exception("operator_status_failed")
                        status, value = 503, {"error": "Database unavailable"}
            else:
                status, value = 404, {"error": "Not found"}
            data = json.dumps(value, default=str).encode()
            self.respond(status, data)

        def log_message(self, *_):
            return

    return ThreadingHTTPServer((host, port), Handler)


def supervise(config):
    failures = 0
    stop_path = Path(os.environ["SPOILLESS_SUPERVISOR_STOP"]) if os.environ.get("SPOILLESS_SUPERVISOR_STOP") else None
    while failures < 8 and not (stop_path and stop_path.exists()):
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-m", "pipeline.worker", "--config", config],
                                env={**os.environ, "SPOILLESS_SUPERVISOR_PID": str(os.getpid()),
                                     "SPOILLESS_SUPERVISOR_PARENT_PID": str(os.getppid())},
                                stdout=sys.stdout, stderr=sys.stderr,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            return
        if time.monotonic() - started >= 300:
            failures = 0
        delay = backoff(failures, jitter=True)
        failures += 1
        LOG.error("worker_process_exited exit_code=%s restart_attempt=%s restart_delay_seconds=%.3f", result.returncode, failures, delay)
        if failures < 8:
            until = time.monotonic() + delay
            while time.monotonic() < until:
                if stop_path and stop_path.exists():
                    return
                time.sleep(min(1, max(0, until - time.monotonic())))
    if failures >= 8:
        raise RuntimeError("Worker repeatedly crashed during startup; automatic restarts exhausted after eight attempts")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("SPOILLESS_PIPELINE_CONFIG"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--migrate-only", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--supervise", action="store_true")
    arguments = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    config = Config.load(arguments.config)
    if arguments.supervise:
        if arguments.once or arguments.check_config or arguments.migrate_only or not arguments.config:
            parser.error("--supervise requires --config and cannot be combined with one-shot commands")
        supervise(arguments.config)
        return
    if arguments.check_config:
        print(json.dumps({"mode": "shadow" if config.shadow else "publish", "configured": True, "processing_slots": config.processing_slots}))
        return
    worker = Worker(Store(config.database_url), config)
    worker.store.migrate()
    if arguments.migrate_only:
        return
    if arguments.once:
        worker.run_once()
        return
    server = status_server(
        worker,
        os.environ.get("SPOILLESS_STATUS_HOST", "127.0.0.1"),
        int(os.environ.get("SPOILLESS_STATUS_PORT", "8767")),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for name in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(name, lambda *_: worker.stop.set())
    try:
        worker.run()
    finally:
        worker.stop.set()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
