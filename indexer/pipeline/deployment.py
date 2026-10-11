import base64
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from psycopg.types.json import Jsonb

from .cli import release_to_site
from .errors import NeedsReview, WaitingSource, WaitingWork
from .publishing import export_snapshot
from .schedule import fetch_bytes
from .storage import LocalStorage


LOG = logging.getLogger(__name__)


class Deployment:
    def __init__(self, store, config):
        self.store = store
        self.config = config

    def git(self, directory, *arguments):
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        token = os.environ.get("SPOILLESS_GITHUB_TOKEN")
        if token:
            credential = base64.b64encode(("x-access-token:" + token).encode()).decode()
            environment.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                               GIT_CONFIG_VALUE_0="Authorization: Basic " + credential)
        result = subprocess.run(["git", "-C", str(directory), *arguments], env=environment,
                                capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise WaitingSource("Website Git operation failed: " + arguments[0] + "; check repository credentials and branch access")
        return result.stdout.strip()

    def publish(self, job):
        settings = (self.config.settings or {}).get("deployment")
        if self.config.shadow or not settings:
            return
        root = self.config.storage_path.resolve() / "deployment"
        root.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            path = Path(temporary)
            checkout = path / "checkout"
            branch = settings.get("branch", "main")
            self.git(path, "clone", "--quiet", "--depth", "1", "--branch", branch,
                     settings["repository"], str(checkout))
            with self.store.transaction() as connection:
                connection.execute("SELECT pg_advisory_xact_lock(87314402)")
                self.store.guard(connection, job)
                approved = [row["youtube_id"] for row in connection.execute(
                    "SELECT youtube_id,channel_id,event,day,metadata FROM pipeline.broadcasts FOR SHARE").fetchall()
                    if self.config.approved(row)]
                connection.execute("SELECT id FROM pipeline.segments WHERE broadcast_id IN (SELECT id FROM pipeline.broadcasts WHERE youtube_id=ANY(%s)) FOR SHARE", (approved,)).fetchall()
                export_snapshot(self.store, LocalStorage(path / "snapshot"), False, approved, connection=connection)
                release = path / "snapshot" / "release"
                previous = json.loads((checkout / "site/catalog.json").read_text(encoding="utf-8"))
                previous_artifacts = self.read_artifacts(previous, checkout / "site")
                release_to_site(release, checkout / "site")
                catalog = json.loads((release / "catalog.json").read_text(encoding="utf-8"))
                validation = subprocess.run(
                    [os.environ.get("NODE_BINARY", "node"), "-e",
                     "const fs=require('fs'),p=require('path');require(process.argv[1]);"
                     "const root=process.argv[2],c=JSON.parse(fs.readFileSync(p.join(root,'catalog.json')));"
                     "for(const e of c.videos){VodlockSite.validateIndex(JSON.parse(fs.readFileSync(p.join(root,e.index))));"
                     "if(e.chat)VodlockSite.validateChat(JSON.parse(fs.readFileSync(p.join(root,e.chat))),e.chatSourceId)}",
                     str(checkout / "site" / "core.js"), str(release)],
                    capture_output=True, text=True, timeout=30)
                if validation.returncode:
                    raise NeedsReview("Website artifact validation rejected the release; publication was not pushed")
                artifacts = {"site/catalog.json"}
                for entry in catalog["videos"]:
                    artifacts.add("site/" + entry["index"].lstrip("/"))
                    if entry.get("chat"):
                        artifacts.add("site/" + entry["chat"].lstrip("/"))
                self.git(checkout, "add", "--", *sorted(artifacts))
                changed = self.git(checkout, "diff", "--cached", "--name-only")
                publication_kind = "ready"
                if changed:
                    current = json.loads((checkout / "site/catalog.json").read_text(encoding="utf-8"))
                    completed_chat = {row["source_id"] for row in connection.execute(
                        """SELECT COALESCE(s.metadata->>'vod_id',s.external_id) AS source_id
                           FROM pipeline.sources s JOIN pipeline.chat_archives c ON c.source_id=s.id
                           WHERE s.provider='twitch' AND s.state='ended' AND c.state='complete'
                           AND NOT EXISTS (SELECT 1 FROM pipeline.jobs j WHERE j.source_id=s.id
                           AND j.kind IN ('twitch_vod','twitch_live','twitch_align','chat_gaps')
                           AND j.state IN ('queued','running','waiting_source','retryable'))""").fetchall()}
                    publication_kind = self.publication_kind(previous, current, previous_artifacts,
                                                             self.read_artifacts(current, checkout / "site"), completed_chat)
                    self.check_budget(connection, settings, publication_kind)
                    self.git(checkout, "-c", "user.name=Spoilless Worker", "-c", "user.email=worker@spoilless.invalid",
                             "commit", "--quiet", "-m", "Publish validated canonical match indexes")
                    if time.monotonic() - started >= self.config.lease_seconds - 50:
                        raise WaitingWork("Website publication yielded before its lease deadline")
                    current = connection.execute(
                        "SELECT id FROM pipeline.jobs WHERE id=%s AND lease_token=%s AND lease_until>clock_timestamp()",
                        (job["id"], job["lease_token"])).fetchone()
                    if not current:
                        raise WaitingWork("Website publication lease expired before push")
                    self.git(checkout, "push", "--quiet", "origin", "HEAD:refs/heads/" + branch)
                commit = self.git(checkout, "rev-parse", "HEAD")
                if not changed and not connection.execute("SELECT commit_sha FROM pipeline.deployments WHERE commit_sha=%s", (commit,)).fetchone():
                    if self.git(checkout, "log", "-1", "--format=%s") != "Publish validated canonical match indexes":
                        return
                connection.execute(
                    """INSERT INTO pipeline.deployments(commit_sha,repository,branch,state,publication_kind,matches)
                       VALUES (%s,%s,%s,'pushed',%s,%s) ON CONFLICT(commit_sha) DO NOTHING""",
                    (commit, settings["repository"], branch, publication_kind,
                     Jsonb([{key: entry[key] for key in ('expectedMatchId','pipelineGeneration','pipelineVersion','pipelineState')}
                            for entry in catalog["videos"] if entry.get('provider') == 'youtube'])))
                record = connection.execute("SELECT state FROM pipeline.deployments WHERE commit_sha=%s", (commit,)).fetchone()
                if record["state"] == "pushed":
                    self.store.enqueue("verify_deployment", "verify-deployment:" + commit,
                                       payload={"commit": commit}, delay=20, connection=connection)
                LOG.info("website_release_pushed job_id=%s commit=%s", job["id"], commit)

    def read_artifacts(self, catalog, site):
        storage = LocalStorage(site)
        return {entry[field]: json.loads(storage.get(entry[field].lstrip("/")))
                for entry in catalog["videos"] if entry.get("canonicalPipeline")
                for field in ("index", "chat") if entry.get(field)}

    def publication_kind(self, previous, current, before, after, completed_chat):
        def entries(catalog):
            return {entry.get("catalogId") or entry["expectedMatchId"]: entry
                    for entry in catalog["videos"] if entry.get("canonicalPipeline")}

        def chat_ready(entry, artifacts):
            if not entry.get("chat") or not artifacts[entry["chat"]].get("messages"):
                return False
            index = artifacts[entry["index"]]
            return any(section["targetStart"] <= value["start"] <= section["targetEnd"]
                       for section in index.get("alignment", {}).get("segments", []) for value in index["rounds"])

        old, new = entries(previous), entries(current)
        if [entry for entry in previous["videos"] if not entry.get("canonicalPipeline")] != [entry for entry in current["videos"] if not entry.get("canonicalPipeline")]:
            return "ready"
        withheld_before = {entry.get("catalogId") or entry["expectedMatchId"]:
                           {name: value for name, value in entry.items() if name != "pipelineAlignmentVersion"}
                           for entry in previous.get("withheld", [])}
        withheld_after = {entry.get("catalogId") or entry["expectedMatchId"]:
                          {name: value for name, value in entry.items() if name != "pipelineAlignmentVersion"}
                          for entry in current.get("withheld", [])}
        if old.keys() != new.keys() or withheld_before != withheld_after:
            return "ready"
        for key, entry in new.items():
            prior = old[key]
            ignored = {"index", "chat", "chatSourceId", "pipelineAlignmentVersion"}
            if {name: value for name, value in entry.items() if name not in ignored} != {name: value for name, value in prior.items() if name not in ignored}:
                return "ready"
            index, prior_index = after[entry["index"]], before[prior["index"]]
            if {name: value for name, value in index.items() if name not in {"alignment", "alignmentVersion"}} != {name: value for name, value in prior_index.items() if name not in {"alignment", "alignmentVersion"}}:
                return "ready"
            usable = chat_ready(entry, after)
            if usable and (not chat_ready(prior, before) or entry.get("chatSourceId") != prior.get("chatSourceId")):
                return "ready"
            if usable and entry.get("chatSourceId") in completed_chat and (index != prior_index or after[entry["chat"]] != before.get(prior.get("chat"))):
                return "ready"
        return "incremental"

    def check_budget(self, connection, settings, publication_kind):
        usage = connection.execute(
            """SELECT COUNT(*) FILTER (WHERE created_at>clock_timestamp()-interval '24 hours') AS total,
               COUNT(*) FILTER (WHERE publication_kind='incremental' AND created_at>clock_timestamp()-interval '24 hours') AS incremental,
               EXTRACT(EPOCH FROM (clock_timestamp()-MAX(created_at) FILTER (WHERE publication_kind='incremental'))) AS elapsed,
               EXTRACT(EPOCH FROM (MIN(created_at) FILTER (WHERE created_at>clock_timestamp()-interval '24 hours')+interval '24 hours'-clock_timestamp())) AS next_total,
               EXTRACT(EPOCH FROM (MIN(created_at) FILTER (WHERE publication_kind='incremental' AND created_at>clock_timestamp()-interval '24 hours')+interval '24 hours'-clock_timestamp())) AS next_incremental
               FROM (SELECT repository,branch,publication_kind,created_at FROM pipeline.deployments
                     UNION ALL SELECT d.repository,d.branch,d.publication_kind,r.created_at FROM pipeline.deployment_retries r
                     JOIN pipeline.deployments d ON d.commit_sha=r.commit_sha) usage WHERE repository=%s AND branch=%s""",
            (settings["repository"], settings.get("branch", "main"))).fetchone()
        limit = settings.get("daily_deployment_limit", 20)
        if usage["total"] >= limit:
            raise WaitingWork("Website publication is waiting for the rolling 24-hour worker budget", retry_after=float(usage.get("next_total") or 3600) + 5)
        if publication_kind == "incremental" and (
            usage["total"] >= max(0, limit - 8)
            or usage["incremental"] >= settings.get("incremental_deployment_limit", 4)
            or usage["elapsed"] is not None and usage["elapsed"] < settings.get("minimum_interval_seconds", 21600)
        ):
            waits = [60.0]
            if usage["total"] >= max(0, limit - 8):
                waits.append(float(usage.get("next_total") or 3600))
            if usage["incremental"] >= settings.get("incremental_deployment_limit", 4):
                waits.append(float(usage.get("next_incremental") or 3600))
            if usage["elapsed"] is not None:
                waits.append(settings.get("minimum_interval_seconds", 21600) - float(usage["elapsed"]))
            raise WaitingWork("Website publication is batching incremental chat and alignment updates", retry_after=max(waits) + 5)

    def verify(self, job):
        record = self.store.one("SELECT * FROM pipeline.deployments WHERE commit_sha=%s", (job["payload"]["commit"],))
        if not record or record["state"] != "pushed":
            return
        newer = self.store.one("SELECT commit_sha FROM pipeline.deployments WHERE repository=%s AND branch=%s AND created_at>%s AND state='deployed' LIMIT 1",
                               (record["repository"], record["branch"], record["created_at"]))
        if newer:
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                connection.execute("UPDATE pipeline.deployments SET state='superseded',updated_at=now() WHERE commit_sha=%s", (record["commit_sha"],))
            return
        repository = record["repository"].removeprefix("https://github.com/").removesuffix(".git")
        headers = {"User-Agent": "Spoilless-Worker", "Accept": "application/vnd.github+json"}
        token = os.environ.get("SPOILLESS_GITHUB_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        value = json.loads(fetch_bytes("https://api.github.com/repos/" + repository +
                                      "/actions/runs?head_sha=" + record["commit_sha"], headers))
        runs = [run for run in value["workflow_runs"] if run.get("path") == ".github/workflows/deploy-site.yml"]
        if not runs or runs[0]["status"] != "completed":
            updated = datetime.fromisoformat(runs[0]["updated_at"].replace("Z", "+00:00")) if runs and runs[0].get("updated_at") else record["created_at"]
            if (datetime.now(timezone.utc) - updated).total_seconds() > 1800:
                raise WaitingSource("Website deployment did not complete within 30 minutes; inspect GitHub Actions and push credentials")
            raise WaitingWork("Website deployment is waiting for GitHub Actions")
        if runs[0]["conclusion"] != "success":
            newer = self.store.one("SELECT commit_sha FROM pipeline.deployments WHERE created_at>%s ORDER BY created_at DESC LIMIT 1", (record["created_at"],))
            if runs[0]["conclusion"] == "cancelled" and newer:
                with self.store.transaction() as connection:
                    self.store.guard(connection, job)
                    connection.execute("UPDATE pipeline.deployments SET state='superseded',workflow_id=%s,updated_at=now() WHERE commit_sha=%s",
                                       (runs[0]["id"], record["commit_sha"]))
                return
            self.retry_failed(job, record, runs[0], repository, headers)
            return
        with self.store.transaction() as connection:
            self.store.guard(connection, job)
            connection.execute("UPDATE pipeline.deployments SET state='deployed',workflow_id=%s,updated_at=now() WHERE commit_sha=%s",
                               (runs[0]["id"], record["commit_sha"]))
            connection.execute("""UPDATE pipeline.jobs SET state='unsupported',failure_kind='superseded',
                last_error='Superseded by a newer verified deployment',updated_at=now()
                WHERE kind='verify_deployment' AND state IN ('needs_review','waiting_source','queued','retryable')
                AND payload->>'commit' IN (SELECT commit_sha FROM pipeline.deployments WHERE repository=%s AND branch=%s AND created_at<%s)""",
                (record["repository"], record["branch"], record["created_at"]))
        LOG.info("website_release_deployed job_id=%s commit=%s", job["id"], record["commit_sha"])

    def retry_failed(self, job, record, run, repository, headers):
        endpoint = "https://api.github.com/repos/" + repository
        head = json.loads(fetch_bytes(endpoint + "/commits/" + record["branch"], headers))
        if head["sha"] != record["commit_sha"]:
            with self.store.transaction() as connection:
                self.store.guard(connection, job)
                connection.execute("UPDATE pipeline.deployments SET state='superseded',workflow_id=%s,updated_at=now() WHERE commit_sha=%s",
                                   (run["id"], record["commit_sha"]))
            return
        jobs = json.loads(fetch_bytes(endpoint + f"/actions/runs/{run['id']}/jobs", headers))["jobs"]
        logs = []
        for value in jobs:
            if value.get("conclusion") == "failure":
                logs.append(fetch_bytes(endpoint + f"/actions/jobs/{value['id']}/logs", headers).decode(errors="replace"))
        evidence = "\n".join(logs).lower()
        quota = "api-deployments-free-per-day" in evidence or "resource is limited - try again in 24 hours" in evidence
        transient = any(message in evidence for message in ["econnreset", "etimedout", "socket hang up", "service unavailable", "too many requests"])
        if not quota and not transient:
            raise NeedsReview("Website build failed for a non-transient reason; review the deploy-site run: " + run["html_url"])
        if "Authorization" not in headers:
            raise NeedsReview("Automatic deployment recovery requires SPOILLESS_GITHUB_TOKEN with Actions write access; failed run: " + run["html_url"])
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))).total_seconds()
        cooldown = 86400 if quota else 600
        if age < cooldown:
            raise WaitingWork("Website deployment is cooling down after a provider quota or network failure", retry_after=cooldown - age + 30)
        workflow = json.loads(fetch_bytes(endpoint + "/contents/.github/workflows/deploy-site.yml?ref=" + record["commit_sha"], headers))
        text = base64.b64decode(workflow["content"]).decode()
        if 'git ls-remote origin "refs/heads/$GITHUB_REF_NAME"' not in text or '"$current_head" != "$GITHUB_SHA"' not in text:
            raise NeedsReview("This release predates the deployment current-head guard; push the reliability workflow update before enabling automatic retries")
        settings = (self.config.settings or {})["deployment"]
        with self.store.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(87314402)")
            self.store.guard(connection, job)
            retries = connection.execute("SELECT * FROM pipeline.deployment_retries WHERE commit_sha=%s ORDER BY created_at", (record["commit_sha"],)).fetchall()
            if len(retries) >= 2 or any(value["run_id"] == run["id"] and value["run_attempt"] == run.get("run_attempt", 1) for value in retries):
                raise NeedsReview("Automatic deployment retries exhausted or their outcome is unconfirmed; review: " + run["html_url"])
            self.check_budget(connection, settings, record["publication_kind"])
            connection.execute("INSERT INTO pipeline.deployment_retries(commit_sha,run_id,run_attempt) VALUES (%s,%s,%s)",
                               (record["commit_sha"], run["id"], run.get("run_attempt", 1)))
        with self.store.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(87314402)")
            self.store.guard(connection, job)
            current = json.loads(fetch_bytes(endpoint + "/commits/" + record["branch"], headers))
            if current["sha"] != record["commit_sha"]:
                raise NeedsReview("The branch changed before the deployment retry; no obsolete release was requested")
            with urlopen(Request(endpoint + f"/actions/runs/{run['id']}/rerun-failed-jobs", data=b"", headers=headers, method="POST"), timeout=30):
                pass
        raise WaitingWork("Website deployment retry requested; waiting for the new GitHub Actions attempt", retry_after=60)
