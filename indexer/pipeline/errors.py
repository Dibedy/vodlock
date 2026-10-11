import errno
import subprocess
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError


class WaitingSource(RuntimeError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class WaitingWork(WaitingSource):
    pass


class NeedsReview(RuntimeError):
    pass


class Unsupported(RuntimeError):
    pass


class StaleAttempt(RuntimeError):
    pass


class ContinueJob(RuntimeError):
    pass


def failure_kind(error):
    if isinstance(error, WaitingSource) and any(value in str(error) for value in [
        "TWITCH_DOWNLOADER is required", "check repository credentials", "requires local TWITCH_USER_TOKEN",
        "requires TWITCH_USER_TOKEN and TWITCH_CLIENT_ID",
    ]):
        return "configuration"
    if isinstance(error, WaitingWork):
        return "dependency"
    if isinstance(error, NeedsReview):
        return "data_quality"
    if isinstance(error, Unsupported):
        return "unsupported"
    if isinstance(error, InterruptedError):
        return "interrupted"
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    if isinstance(error, HTTPError):
        if error.code == 429:
            return "rate_limit"
        if error.code in {401, 403}:
            return "configuration"
        if error.code in {404, 410}:
            return "source_unavailable"
        return "network" if error.code >= 500 or error.code == 408 else "unexpected"
    if isinstance(error, (ConnectionError, URLError)):
        return "network"
    if isinstance(error, MemoryError) or isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.ENOMEM, errno.EMFILE, errno.EAGAIN}:
        return "resource_limit"
    if isinstance(error, WaitingSource):
        return "source_unavailable"
    return "unexpected"


def retry_after(error):
    if isinstance(error, WaitingSource):
        return error.retry_after
    if isinstance(error, HTTPError) and error.headers:
        value = error.headers.get("Retry-After")
        if value:
            try:
                return max(0, min(86400, int(value)))
            except ValueError:
                try:
                    return max(0, min(86400, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
                except (ValueError, TypeError):
                    return None
    return None
