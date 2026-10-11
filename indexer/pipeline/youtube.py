import json
import logging
import math
import os
import time
from copy import deepcopy
from datetime import datetime, timezone
from itertools import islice
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.etree.ElementTree import ParseError

from storyboard_align import extractor_options
from server import apply_youtube_options

from .errors import WaitingSource
from .schedule import fetch_bytes


LOG = logging.getLogger(__name__)


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value, timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def metadata_state(value):
    details = value.get("liveStreamingDetails", {})
    status = value.get("live_status") or value.get("snippet", {}).get("liveBroadcastContent")
    if status in {"is_upcoming", "upcoming"}:
        state = "scheduled"
    elif status in {"is_live", "live"} and not details.get("actualEndTime"):
        state = "live"
    elif status in {"post_live", "was_live"} or details.get("actualEndTime"):
        state = "ended_waiting_archive"
    else:
        state = "discovered"
    return {
        "state": state,
        "actual_start": timestamp(
            details.get("actualStartTime")
            or (value.get("release_timestamp") if status in {"is_live", "was_live", "post_live"} else None)
        ),
        "actual_end": timestamp(details.get("actualEndTime")),
        "metadata": value,
    }


class YouTube:
    def __init__(self, requester=fetch_bytes, archive_client=None):
        self.requester = requester
        self.archive_client = archive_client
        self.rejected_media: dict[str, float] = {}
        self.rss_retry_at: dict[str, float] = {}
        self.rss_feeds: dict[str, tuple[float, bytes]] = {}
        self.media_cache: dict[tuple, tuple[float, dict]] = {}

    def invalidate(self, video_id=None):
        self.media_cache = {key: value for key, value in self.media_cache.items() if video_id is not None and key[0] != video_id}

    def reject(self, video_id):
        self.invalidate(video_id)
        self.rejected_media[video_id] = time.monotonic() + 1800

    def discover(self, channel, uploads=False):
        from auto_publish import discover_youtube, is_candidate
        import yt_dlp

        channel_id = channel["channel_id"]
        legacy = {**channel, "channelId": channel_id, "canonicalStream": True, "minimumDuration": 0}
        now = time.monotonic()
        if now >= self.rss_retry_at.get(channel_id, 0):
            try:
                cached = self.rss_feeds.get(channel_id)
                feed = cached[1] if cached and now - cached[0] < 30 else self.requester(
                    "https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id
                )
                entries = discover_youtube(legacy, 30, yt_dlp=yt_dlp, requester=lambda _: feed)
                self.rss_feeds[channel_id] = (now, feed)
                return entries
            except (URLError, TimeoutError, ParseError) as error:
                self.rss_feeds.pop(channel_id, None)
                self.rss_retry_at[channel_id] = now + 1800
                LOG.warning(
                    "youtube_rss_unavailable channel_id=%s status=%s fallback=yt_dlp retry_seconds=1800 reason=%s",
                    channel_id, getattr(error, "code", "network_or_xml"), error,
                )
        tab = "videos" if uploads else "streams"
        options = {
            **extractor_options("youtube"), "extract_flat": "in_playlist", "playlistend": 30,
            "skip_download": True, "socket_timeout": 20, "retries": 1, "logger": LOG,
        }
        apply_youtube_options(options)
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                result = downloader.extract_info(f"https://www.youtube.com/channel/{channel_id}/{tab}", download=False)
        except yt_dlp.utils.DownloadError as error:
            raise WaitingSource(f"YouTube discovery unavailable for channel {channel_id} tab {tab}: {error}") from error
        if not result or "entries" not in result:
            raise WaitingSource(f"YouTube discovery returned no listing for channel {channel_id} tab {tab}")
        if result.get("channel_id") not in {None, channel_id}:
            raise WaitingSource(f"YouTube discovery channel identity mismatch for {channel_id}")
        entries = {}
        for entry in islice(result["entries"] or [], 30):
            if entry and is_candidate(legacy, {**entry, "live_status": None}, require_duration=False):
                entries.setdefault(entry["id"], entry)
        return list(entries.values())

    def metadata(self, video_id):
        key = os.environ.get("YOUTUBE_API_KEY")
        if key:
            query = urlencode({"part": "snippet,liveStreamingDetails,contentDetails", "id": video_id, "key": key})
            value = json.loads(self.requester("https://www.googleapis.com/youtube/v3/videos?" + query))
            if not value.get("items"):
                return {"state": "unavailable", "actual_start": None, "actual_end": None, "metadata": {}}
            return metadata_state(value["items"][0])
        import yt_dlp

        options = {
            **extractor_options("youtube"),
            "skip_download": True,
            "ignore_no_formats_error": True,
            "extract_flat": True,
            "socket_timeout": 20,
            "retries": 1,
        }
        apply_youtube_options(options)
        with yt_dlp.YoutubeDL(options) as downloader:
            value = downloader.extract_info("https://www.youtube.com/watch?v=" + video_id, download=False)
        if not value:
            raise WaitingSource("YouTube metadata is temporarily unavailable")
        result = metadata_state(value)
        result["metadata"] = {
            key: value.get(key) for key in ["id", "title", "channel_id", "live_status", "duration", "release_timestamp"]
        }
        return result

    def resolve(self, video_id, live=False, height=540, cached=False, minimum_height=0):
        import yt_dlp

        now = time.monotonic()
        self.media_cache = {key: value for key, value in self.media_cache.items() if value[0] > now}
        client = self.archive_client or ("android" if self.rejected_media.get(video_id, 0) > now else None)
        if live:
            client = None
        key = (video_id, height, minimum_height, os.environ.get("VODLOCK_YOUTUBE_COOKIES", ""), os.environ.get("VODLOCK_YOUTUBE_POT", ""), client)
        if cached and not live and key in self.media_cache:
            LOG.debug("youtube_media_cache_hit youtube_id=%s height=%s", video_id, height)
            return deepcopy(self.media_cache[key][1])
        if not cached or live:
            self.invalidate(video_id)
        url = "https://www.youtube.com/watch?v=" + video_id
        selector = (
            f"bestvideo[protocol^=m3u8][height<={height}]/best[protocol^=m3u8][height<={height}]/"
            "bestvideo[protocol^=m3u8][height<=720]/best[protocol^=m3u8][height<=720]"
            if live
            else f"bestvideo[height<={height}]/best[height<={height}]"
        )
        options = {**extractor_options("youtube", selector), "socket_timeout": 20, "retries": 1, "noplaylist": True}
        apply_youtube_options(options)
        public_options = deepcopy(options)
        if client == "android":
            options.pop("cookiefile", None)
            options.setdefault("extractor_args", {})["youtube"] = {"player_client": [client]}
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=False)
            if not live and client == "android" and info and (info.get("height") or 0) < minimum_height:
                with yt_dlp.YoutubeDL(public_options) as downloader:
                    info = downloader.extract_info(url, download=False)
                client = None
        except yt_dlp.utils.DownloadError as error:
            message = str(error)
            if not live and client != "android" and any(token in message.lower() for token in ["403", "requested format", "no video formats"]):
                self.reject(video_id)
            if any(
                token in message.lower()
                for token in [
                    "no video formats",
                    "requested format",
                    "live event has ended",
                    "not yet available",
                    "403",
                    "404",
                ]
            ):
                raise WaitingSource("YouTube media is not ready; refresh metadata/URL before retry") from error
            raise
        if not info or not info.get("url"):
            raise WaitingSource("YouTube has no processable media URL yet")
        if (info.get("height") or 0) < minimum_height:
            raise WaitingSource(f"YouTube recovery requires at least {minimum_height}p video; available resolution is insufficient")
        remote = {
            "url": info["url"],
            "headers": info.get("http_headers", {}),
            "duration": info.get("duration"),
            "actual_start": timestamp(info.get("release_timestamp")),
            "height": info.get("height"),
            "player_client": client,
            "cache_identity": ("youtube", video_id, info["format_id"], info.get("height"), info.get("duration"))
            if not live and not info.get("is_live") and info.get("live_status") != "is_live"
            and info.get("format_id") and info.get("duration") else None,
        }
        if not live and not info.get("is_live") and info.get("live_status") != "is_live":
            lifetime = 300.0
            expiry = parse_qs(urlsplit(info["url"]).query).get("expire", [])
            if expiry:
                try:
                    expiration = float(expiry[0])
                    lifetime = min(lifetime, expiration - time.time() - 180) if math.isfinite(expiration) else 0
                except ValueError:
                    lifetime = 0
            if lifetime > 0:
                if len(self.media_cache) >= 128:
                    self.media_cache.pop(next(iter(self.media_cache)))
                self.media_cache[key] = (time.monotonic() + lifetime, deepcopy(remote))
        return remote
