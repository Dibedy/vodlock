import json
import math
import os
from datetime import date
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    database_url: str
    shadow: bool = True
    lease_seconds: int = 180
    poll_seconds: int = 5
    discovery_seconds: int = 120
    fingerprint_seconds: int = 10
    minimum_confidence: float = 0.85
    maximum_residual: float = 2
    storage_path: Path = Path("indexer/data/pipeline")
    settings: dict | None = None
    processing_slots: int = 2

    @classmethod
    def load(cls, path=None):
        settings = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        if settings.get("youtube_archive_client") not in {None, "android"}:
            raise ValueError("youtube_archive_client must be android or omitted")
        slots = settings.get("processing_slots", 2)
        if isinstance(slots, bool) or not isinstance(slots, int) or slots not in {1, 2}:
            raise ValueError("processing_slots must be 1 or 2")
        starts = settings.get("archive_start_seconds", {})
        if not isinstance(starts, dict) or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
            for value in starts.values()
        ):
            raise ValueError("archive_start_seconds must map YouTube video IDs to finite nonnegative seconds")
        database = os.environ.get("DATABASE_URL", "")
        if not database:
            raise ValueError("DATABASE_URL is required for the persistent worker")
        mode = os.environ.get("SPOILLESS_PIPELINE_MODE", "shadow")
        if mode not in {"shadow", "publish"}:
            raise ValueError("SPOILLESS_PIPELINE_MODE must be shadow or publish")
        for rule in settings.get("auto_publish", []):
            if not isinstance(rule, dict) or not all(rule.get(key) for key in ("channel_id", "event", "from_day")):
                raise ValueError("auto_publish requires channel_id, event and from_day")
            date.fromisoformat(rule["from_day"])
            if rule.get("through_day"):
                if date.fromisoformat(rule["through_day"]) < date.fromisoformat(rule["from_day"]):
                    raise ValueError("auto_publish through_day precedes from_day")
            if not any(channel["channel_id"] == rule["channel_id"] and channel.get("event") == rule["event"]
                       for channel in settings.get("youtube_channels", [])):
                raise ValueError("auto_publish must identify a configured official YouTube channel and event")
        deployment = settings.get("deployment")
        if deployment:
            import re

            interval = deployment.get("minimum_interval_seconds", 21600)
            if isinstance(interval, bool) or not isinstance(interval, int) or interval < 3600:
                raise ValueError("deployment minimum_interval_seconds must be an integer of at least 3600")
            for key, default, minimum, maximum in (("daily_deployment_limit", 20, 1, 30), ("incremental_deployment_limit", 4, 0, 4)):
                value = deployment.get(key, default)
                if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                    raise ValueError(f"deployment {key} must be an integer between {minimum} and {maximum}")
            if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", deployment.get("repository", "")):
                raise ValueError("deployment repository must be a GitHub HTTPS repository")
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_/-]*", deployment.get("branch", "main")):
                raise ValueError("Invalid deployment branch")
        if mode == "publish" and not settings.get("approved_broadcasts") and not settings.get("auto_publish"):
            raise ValueError("Publish mode requires explicitly reviewed approved_broadcasts in the configuration")
        return cls(
            database,
            shadow=mode == "shadow",
            settings=settings,
            processing_slots=slots,
            storage_path=Path(os.environ.get("SPOILLESS_STORAGE_PATH", "indexer/data/pipeline")),
        )

    def shadow_for(self, broadcast):
        return self.shadow or not self.approved(broadcast)

    def approved(self, broadcast):
        settings = self.settings or {}
        if broadcast["youtube_id"] in settings.get("approved_broadcasts", []):
            return True
        if not settings.get("auto_publish"):
            return False
        day = str(broadcast["day"])
        metadata = broadcast.get("metadata", {})
        verified_channel = metadata.get("channel_id") or metadata.get("snippet", {}).get("channelId")
        return any(
            broadcast["channel_id"] == rule["channel_id"] and broadcast["event"] == rule["event"]
            and verified_channel == rule["channel_id"]
            and rule["from_day"] <= day and (not rule.get("through_day") or day <= rule["through_day"])
            for rule in settings.get("auto_publish", [])
        )

    def approved_ids(self, store):
        return [row["youtube_id"] for row in store.rows("SELECT youtube_id,channel_id,event,day,metadata FROM pipeline.broadcasts")
                if self.approved(row)]

    def archive_start_for(self, broadcast):
        return float((self.settings or {}).get("archive_start_seconds", {}).get(broadcast["youtube_id"], 0))
