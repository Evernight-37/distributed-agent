"""Scheduled release check against this project's GitHub Releases.

The application ships without telemetry and is upgraded by downloading a release
by hand, so this check is deliberately small: it compares the latest published
tag with the running version, never downloads anything, and reports the release
page for the user to open. A machine without network simply records nothing and
tries again later; the whole feature is off when the schedule is ``never``.
"""

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from agent_harness.common import read_json, write_json
from agent_harness.release_info import version as application_version

REPOSITORY = "Evernight-37/distributed-agent"
RELEASES_PAGE = "https://github.com/" + REPOSITORY + "/releases/latest"
LATEST_API = "https://api.github.com/repos/" + REPOSITORY + "/releases/latest"

# Seconds between checks; ``never`` disables the feature entirely.
SCHEDULES = {"never": None, "daily": 86400, "weekly": 604800, "monthly": 2592000}
DEFAULT_SCHEDULE = "weekly"
# An offline attempt must not turn every window focus into another request.
OFFLINE_RETRY_SECONDS = 3600
ALLOWED_URL_HOSTS = {"github.com", "www.github.com"}


def settings_path(root):
    return Path(root) / "updates.json"


def load(root):
    value = read_json(settings_path(root), {})
    if not isinstance(value, dict):
        value = {}
    schedule = value.get("schedule")
    result = {"schedule": schedule if schedule in SCHEDULES else DEFAULT_SCHEDULE}
    for key in ("last_checked", "last_attempt"):
        if isinstance(value.get(key), (int, float)) and not isinstance(value.get(key), bool):
            result[key] = float(value[key])
    for key in ("latest_version", "url"):
        if isinstance(value.get(key), str) and value[key]:
            result[key] = value[key]
    return result


def save(root, document):
    write_json(settings_path(root), document)


def set_schedule(root, schedule):
    if schedule not in SCHEDULES:
        raise ValueError("更新检查频率只能是 daily、weekly、monthly 或 never")
    document = load(root)
    document["schedule"] = schedule
    save(root, document)
    return document


def interval(schedule):
    return SCHEDULES.get(schedule)


def due(document, now=None):
    """Whether a scheduled check is owed right now."""
    now = time.time() if now is None else now
    span = interval(document.get("schedule"))
    if span is None:
        return False
    if now - float(document.get("last_checked") or 0) < span:
        return False
    return now - float(document.get("last_attempt") or 0) >= OFFLINE_RETRY_SECONDS


def version_tuple(value):
    """Parse ``v2.1.0``, ``2.1.0`` and ``2.1.0-rc.1`` into a comparable tuple."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value or "")
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(latest, current):
    left, right = version_tuple(latest), version_tuple(current)
    if left is None or right is None:
        return False
    return left > right


def fetch_latest(timeout=8):
    """Read the newest published release tag; raises on any failure."""
    request = urllib.request.Request(
        LATEST_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-harness/" + application_version(),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("unexpected release payload")
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        raise ValueError("release has no tag")
    url = payload.get("html_url")
    if not isinstance(url, str) or not safe_url(url):
        url = RELEASES_PAGE
    return {"tag": tag.strip(), "url": url}


def safe_url(value):
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in ALLOWED_URL_HOSTS


def status(root, force=False, fetch=None, now=None):
    """Return the update state, performing a due check when one is owed.

    ``force`` ignores the schedule interval but never ignores ``never``.
    """
    current = application_version()
    document = load(root)
    result = {
        "schedule": document["schedule"],
        "current": current,
        "latest": document.get("latest_version"),
        "url": document.get("url") or RELEASES_PAGE,
        "checked_at": document.get("last_checked"),
    }
    if document["schedule"] == "never":
        return {**result, "status": "disabled"}
    if not force and not due(document, now):
        return {**result, "status": "idle"}
    now = time.time() if now is None else now
    try:
        release = (fetch or fetch_latest)()
    except Exception:
        # No network, rate limit or a private repository: report nothing, and do
        # not pretend the check happened.
        document["last_attempt"] = now
        save(root, document)
        return {**result, "status": "offline"}
    # The page is what a user will be sent to, so never trust it unchecked.
    url = release.get("url") if isinstance(release, dict) else None
    if not isinstance(url, str) or not safe_url(url):
        url = RELEASES_PAGE
    document["last_checked"] = now
    document["last_attempt"] = now
    document["latest_version"] = release["tag"]
    document["url"] = url
    save(root, document)
    found = is_newer(release["tag"], current)
    return {
        **result,
        "status": "update" if found else "current",
        "latest": release["tag"],
        "url": url,
        "checked_at": now,
    }


def release_url(root):
    """The stored release page, validated before anything opens a browser."""
    document = load(root)
    url = document.get("url") or RELEASES_PAGE
    if not safe_url(url):
        raise ValueError("保存的发布页地址无效")
    return url
