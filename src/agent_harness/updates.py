"""Scheduled release check against this project's GitHub Releases.

The application ships without telemetry and is upgraded by downloading a release
by hand, so this check is deliberately small: it compares the latest published
tag with the running version, never downloads anything, and reports the release
page for the user to open. A machine without network simply records nothing and
tries again later; the whole feature is off when the schedule is ``never``.
"""

import json
import re
import threading
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
        "staged": staged(root),
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


# --- downloading one release artifact -------------------------------------
#
# Downloading is deliberately separate from applying it: this module only
# fetches bytes, verifies them and stages them. Replacing an installed
# application is the next step and never happens here.

TAG_API = "https://api.github.com/repos/" + REPOSITORY + "/releases/tags/"
PENDING_DIR = "updates"


def asset_name(version, system=None, machine=None):
    """The artifact this machine would install, for one version."""
    import platform
    import sys

    system = system or sys.platform
    machine = (machine or platform.machine() or "").lower()
    version = str(version).lstrip("v")
    if system in ("win32", "windows"):
        return "AgentHarness-%s-windows-x64-setup.exe" % version
    if system == "darwin":
        return "AgentHarness-%s-macos-%s.zip" % (version, "arm64" if machine in {"arm64", "aarch64"} else "x64")
    return "AgentHarness-%s-linux-%s.tar.gz" % (version, "arm64" if machine in {"aarch64", "arm64"} else "x64")


def select_asset(assets, version, system=None, machine=None):
    """Pick this machine's artifact and the checksum published beside it."""
    wanted = asset_name(version, system, machine)
    found = None
    checksum_url = None
    if not isinstance(assets, list):
        raise ValueError("发布信息里没有资产列表")
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name == wanted:
            url = item.get("browser_download_url")
            if not isinstance(url, str) or not safe_url(url):
                raise ValueError("安装包地址无效")
            found = item
        elif name == wanted + ".sha256":
            url = item.get("browser_download_url")
            if isinstance(url, str) and safe_url(url):
                checksum_url = url
    if found is None:
        raise ValueError("这个版本没有适用于本机的安装包：" + wanted)
    digest = found.get("digest")
    sha256 = None
    if isinstance(digest, str) and digest.startswith("sha256:"):
        sha256 = digest.split(":", 1)[1].strip().lower()
    size = found.get("size")
    return {
        "name": wanted,
        "url": found["browser_download_url"],
        "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
        "sha256": sha256,
        "checksum_url": checksum_url,
    }


def fetch_release(tag, timeout=8):
    """Read one release's asset list, including the digest GitHub publishes."""
    from urllib.parse import quote

    request = urllib.request.Request(
        TAG_API + quote(str(tag), safe=""),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-harness/" + application_version(),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("unexpected release payload")
    return {"tag": str(tag), "assets": payload.get("assets") or []}


def pending_dir(root):
    return Path(root) / PENDING_DIR / "pending"


def staged(root):
    """The artifact already downloaded and verified, if any."""
    record = read_json(pending_dir(root) / "meta.json", None)
    if not isinstance(record, dict):
        return None
    path = pending_dir(root) / str(record.get("file") or "")
    if not record.get("file") or not path.is_file():
        return None
    return record


def _sha256_file(path):
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_staged(root):
    """Re-hash a staged artifact; returns None when it is intact."""
    record = staged(root)
    if not record:
        return "没有已下载的更新"
    path = pending_dir(root) / record["file"]
    if _sha256_file(path).lower() != str(record.get("sha256")).lower():
        return "已下载的安装包与校验值不一致"
    return None


def _read_checksum(url, opener, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "agent-harness/" + application_version()})
    with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
        text = response.read(4096).decode("utf-8", "replace")
    token = text.strip().split()[0] if text.strip() else ""
    return token.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", token) else None


def _stream(url, destination, opener, timeout, size, on_progress):
    import hashlib

    request = urllib.request.Request(url, headers={"User-Agent": "agent-harness/" + application_version()})
    digest = hashlib.sha256()
    received = 0
    with (opener or urllib.request.urlopen)(request, timeout=timeout) as response, destination.open("wb") as stream:
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            stream.write(chunk)
            digest.update(chunk)
            received += len(chunk)
            on_progress(received, size)
    return digest.hexdigest(), received


_STATE = {}
_STATE_LOCK = threading.Lock()


def _key(root):
    return str(Path(root).resolve())


def _set_state(root, **values):
    with _STATE_LOCK:
        state = _STATE.setdefault(_key(root), {})
        state.update(values)


def download_state(root):
    with _STATE_LOCK:
        state = dict(_STATE.get(_key(root)) or {"status": "idle"})
    record = staged(root)
    if record and state.get("status") not in {"downloading", "verifying"}:
        state.update(
            status="ready",
            version=record.get("version"),
            sha256=record.get("sha256"),
            path=str(pending_dir(root) / record["file"]),
            total=record.get("size"),
        )
    return state


def discard(root):
    """Throw away a staged artifact and forget the download state."""
    import shutil

    directory = pending_dir(root)
    shutil.rmtree(directory, ignore_errors=True)
    with _STATE_LOCK:
        _STATE.pop(_key(root), None)
    return {"discarded": True}


def download(root, tag=None, fetch=None, opener=None, timeout=30, progress=None, system=None, machine=None):
    """Download, verify and stage one release artifact (blocking)."""
    document = load(root)
    version = tag or document.get("latest_version")
    if not version:
        raise ValueError("还没有检查到可下载的版本")
    release = (fetch or fetch_release)(version)
    asset = select_asset(release.get("assets"), version, system, machine)
    sha256 = asset["sha256"]
    if not sha256 and asset.get("checksum_url"):
        sha256 = _read_checksum(asset["checksum_url"], opener, timeout)
    if not sha256:
        raise ValueError("发布页没有提供这个安装包的校验值，已停止下载")
    directory = pending_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    partial = directory / (asset["name"] + ".part")
    target = directory / asset["name"]
    try:
        digest, received = _stream(
            asset["url"], partial, opener, timeout, asset.get("size"), progress or (lambda a, b: None)
        )
        if digest.lower() != sha256:
            raise ValueError("安装包校验失败，已删除下载内容")
        if asset.get("size") and received != asset["size"]:
            raise ValueError("安装包大小与发布信息不符，已删除下载内容")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    record = {
        "version": str(version).lstrip("v"),
        "tag": str(version),
        "file": target.name,
        "size": received,
        "sha256": sha256,
        "downloaded_at": time.time(),
    }
    write_json(directory / "meta.json", record)
    return record


def start_download(root, **kwargs):
    """Run one download in the background and expose its progress."""
    import threading

    state = download_state(root)
    if state.get("status") == "downloading":
        return state
    _set_state(root, status="downloading", received=0, total=None, error=None, path=None, sha256=None, version=None)

    def report(received, size):
        _set_state(root, status="downloading", received=received, total=size)

    def run():
        try:
            record = download(root, progress=report, **kwargs)
        except Exception as exc:
            _set_state(root, status="failed", error=str(exc))
            return
        _set_state(
            root,
            status="ready",
            version=record["version"],
            sha256=record["sha256"],
            path=str(pending_dir(root) / record["file"]),
            total=record["size"],
        )

    threading.Thread(target=run, name="update-download", daemon=True).start()
    return download_state(root)
