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


def install_layout(bundle=None):
    """How this copy of the application is installed.

    ``installed`` means an Inno Setup install, wherever it was placed: the
    uninstaller next to the executable is the reliable evidence, since the
    destination is no longer fixed. Anything else is an unpacked portable
    directory, which is updated by unpacking a zip over it.
    """
    import os

    if system_platform() == "darwin":
        return "bundle"
    if os.name != "nt":
        return "portable"
    from agent_harness.service_install import application_bundle

    root = Path(bundle or application_bundle()).resolve()
    if root.is_dir() and any(root.glob("unins*.exe")):
        return "installed"
    return "portable"


def system_platform():
    import sys

    return sys.platform


def asset_name(version, system=None, machine=None, layout=None):
    """The artifact this machine would install, for one version."""
    import platform

    system = system or system_platform()
    machine = (machine or platform.machine() or "").lower()
    version = str(version).lstrip("v")
    if system in ("win32", "windows"):
        if layout == "portable":
            return "AgentHarness-%s-windows-x64.zip" % version
        return "AgentHarness-%s-windows-x64-setup.exe" % version
    if system == "darwin":
        return "AgentHarness-%s-macos-%s.zip" % (version, "arm64" if machine in {"arm64", "aarch64"} else "x64")
    return "AgentHarness-%s-linux-%s.tar.gz" % (version, "arm64" if machine in {"aarch64", "arm64"} else "x64")


def select_asset(assets, version, system=None, machine=None, layout=None):
    """Pick this machine's artifact and the checksum published beside it."""
    wanted = asset_name(version, system, machine, layout)
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


def download(
    root, tag=None, fetch=None, opener=None, timeout=30, progress=None, system=None, machine=None, layout=None
):
    """Download, verify and stage one release artifact (blocking)."""
    document = load(root)
    version = tag or document.get("latest_version")
    if not version:
        raise ValueError("还没有检查到可下载的版本")
    release = (fetch or fetch_release)(version)
    asset = select_asset(release.get("assets"), version, system, machine, layout)
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


# --- applying a staged artifact -------------------------------------------
#
# Replacing an installed application cannot happen from inside it: on Windows a
# running executable cannot be overwritten, and on macOS the bundle is open. The
# application therefore writes a small launcher *outside* the install directory
# (the data root survives upgrades) and exits; the launcher waits for the
# process to disappear, installs, and starts the new version again.
#
# Nothing here downloads anything: the caller passes an already verified file.

APPLY_MARKER = "applied.json"
# How long the launcher waits for the application to exit, in seconds. A PID can
# be reused by the system, so an unbounded wait can spin forever on an unrelated
# process; giving up is safer than leaving a hidden watcher behind.
WAIT_LIMIT = 600


def apply_marker_path(root):
    return Path(root) / PENDING_DIR / APPLY_MARKER


def helper_path(root, version):
    system = __import__("sys").platform
    suffix = ".cmd" if system in ("win32", "windows") else ".sh"
    return Path(root) / PENDING_DIR / ("apply-" + str(version).lstrip("v") + suffix)


def windows_helper(installer, target, pid, directory=None):
    """A .cmd that waits for the caller, installs silently, then relaunches.

    ``/DIR`` pins the silent install to the directory that is already in use: the
    installer is allowed to be placed anywhere now, so letting it fall back to its
    default would create a second copy somewhere else.
    """
    newline = chr(13) + chr(10)
    command = 'call "{installer}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
    if directory:
        command += ' /DIR="{directory}"'
    return (
        newline.join(
            [
                "@echo off",
                "setlocal",
                "set tries=0",
                ":wait_for_exit",
                'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul',
                "if errorlevel 1 goto run_install",
                "set /a tries=tries+1",
                "if %tries% GEQ " + str(WAIT_LIMIT) + " goto give_up",
                "ping -n 2 127.0.0.1 >nul",
                "goto wait_for_exit",
                ":run_install",
                command,
                'start "" "{target}"',
                "exit /b 0",
                ":give_up",
                "exit /b 1",
            ]
        )
        + newline
    ).format(pid=int(pid), installer=installer, target=target, directory=directory or "")


def windows_zip_helper(archive, target, launcher, pid):
    """Windows portable layout: unpack the zip over the directory, then relaunch.

    The release archive keeps everything under a single top-level directory, so
    extracting straight into the target would nest a second copy instead of
    replacing the first. The launcher extracts beside it, unwraps that single
    directory when there is one, and copies the result over the target.

    A portable copy has no installer to run, so the only honest update is to put
    the new files where the old ones were. Files removed in the new version are
    not deleted; that is a known limitation of overwriting in place.
    """
    newline = chr(13) + chr(10)
    extraction = (
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
        "$ErrorActionPreference='Stop'; "
        "$staging=Join-Path $env:TEMP ('ah-update-'+[guid]::NewGuid().ToString('N')); "
        "Expand-Archive -LiteralPath '{archive}' -DestinationPath $staging -Force; "
        "$items=@(Get-ChildItem -LiteralPath $staging); "
        "if ($items.Count -eq 1 -and $items[0].PSIsContainer) {{ $staging=$items[0].FullName }}; "
        "Copy-Item -Path (Join-Path $staging '*') -Destination '{target}' -Recurse -Force; "
        'Remove-Item -LiteralPath $staging -Recurse -Force"'
    )
    return (
        newline.join(
            [
                "@echo off",
                "setlocal",
                "set tries=0",
                ":wait_for_exit",
                'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul',
                "if errorlevel 1 goto run_update",
                "set /a tries=tries+1",
                "if %tries% GEQ " + str(WAIT_LIMIT) + " goto give_up",
                "ping -n 2 127.0.0.1 >nul",
                "goto wait_for_exit",
                ":run_update",
                extraction,
                'start "" "{launcher}"',
                "exit /b 0",
                ":give_up",
                "exit /b 1",
            ]
        )
        + newline
    ).format(pid=int(pid), archive=archive, target=target, launcher=launcher)


def unix_helper(archive, target, pid):
    """A sh script that waits for the caller, unpacks the zip, then reopens."""
    return (
        chr(10).join(
            [
                "#!/bin/sh",
                "tries=0",
                "while kill -0 {pid} 2>/dev/null; do",
                "  tries=$((tries + 1))",
                '  [ "$tries" -ge ' + str(WAIT_LIMIT) + " ] && exit 1",
                "  sleep 1",
                "done",
                "staging=$(mktemp -d) || exit 1",
                'if ! ditto -x -k "{archive}" "$staging" 2>/dev/null; then',
                '  unzip -q "{archive}" -d "$staging" || exit 1',
                "fi",
                'bundle=$(find "$staging" -maxdepth 1 -name "*.app" | head -n 1)',
                'if [ -z "$bundle" ]; then rm -rf "$staging"; exit 1; fi',
                'rm -rf "{target}" && cp -R "$bundle" "{target}"',
                'open "{target}"',
                'rm -rf "$staging"',
            ]
        )
        + chr(10)
    ).format(pid=int(pid), archive=archive, target=target)


def _prune_helpers(root, keep=None):
    """Delete launchers from previous applies; they never run twice."""
    directory = pending_dir(root)
    if not directory.is_dir():
        return
    for path in directory.glob("apply-*"):
        if keep is not None and path == keep:
            continue
        if path.suffix in {".cmd", ".sh"}:
            path.unlink(missing_ok=True)


def apply_target(bundle=None):
    """The path the launcher replaces, and the program it starts afterwards."""
    import sys

    if system_platform() == "darwin":
        executable = Path(bundle or sys.executable).resolve()
        for parent in executable.parents:
            if parent.suffix == ".app":
                return parent, executable
        return executable.parent, executable
    from agent_harness.service_install import application_bundle

    directory = Path(bundle or application_bundle()).resolve()
    return directory, directory / "AgentHarness.exe"


def write_helper(root, version, installer, target, pid, layout=None, launcher=None):
    """Write the launcher for one apply; returns its path."""
    import os

    layout = layout or install_layout(target)
    launcher = launcher or target
    _prune_helpers(root)
    path = helper_path(root, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and layout == "installed":
        text = windows_helper(str(installer), str(launcher), pid, str(target))
    elif os.name == "nt":
        text = windows_zip_helper(str(installer), str(target), str(launcher), pid)
    else:
        text = unix_helper(str(installer), str(target), pid)
    path.write_text(text, encoding="utf-8", newline="")
    if os.name != "nt":
        path.chmod(0o700)
    return path


def apply_state(root):
    """Whether a previous apply is waiting to be confirmed by the next start."""
    record = read_json(apply_marker_path(root), None)
    if not isinstance(record, dict) or not record.get("to_version"):
        return None
    return record


def mark_running(root):
    """Confirm the applied version is the one now running, then forget it.

    A mismatch keeps the marker: it is the only record of which version to fall
    back to, and the settings page offers that fallback exactly in this case.
    Clearing it here made the offer unusable — by the time the user clicked, the
    version it needed was gone.
    """
    record = apply_state(root)
    if not record:
        return None
    if str(record.get("to_version")).lstrip("v") == str(application_version()).lstrip("v"):
        apply_marker_path(root).unlink(missing_ok=True)
        return {"confirmed": True, "version": record.get("to_version")}
    return {"confirmed": False, "expected": record.get("to_version"), "running": application_version()}


def dismiss_apply(root):
    """Forget a failed apply after the user has decided what to do about it."""
    apply_marker_path(root).unlink(missing_ok=True)
    return {"dismissed": True}


def apply(root, installer=None, version=None, target=None, pid=None, force=False, busy=None, spawn=None):
    """Launch the helper that installs a verified artifact after this exits."""
    import os
    import subprocess

    record = staged(root) if installer is None else {"file": Path(installer).name, "version": version}
    if not record:
        raise ValueError("没有已下载并校验的更新")
    installer_path = Path(installer) if installer else pending_dir(root) / record["file"]
    if not installer_path.is_file():
        raise ValueError("安装包不存在，请重新下载")
    if installer is None:
        problem = verify_staged(root)
        if problem:
            raise ValueError(problem)
    if busy:
        active = [item for item in busy() if item]
        if active and not force:
            raise ValueError("正在进行的任务会中断：" + "、".join(active[:3]))
    to_version = str(version or record.get("version") or "").lstrip("v")
    target, launcher = apply_target(target)
    layout = install_layout(target)
    pid = pid or os.getpid()
    script = write_helper(root, to_version, installer_path, target, pid, layout=layout, launcher=launcher)
    write_json(
        apply_marker_path(root),
        {
            "from_version": application_version(),
            "to_version": to_version,
            "installer": str(installer_path),
            "helper": str(script),
            "target": str(target),
            "layout": layout,
            "requested_at": time.time(),
        },
    )
    if spawn is None:
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
            command = ["cmd.exe", "/c", str(script)]
            spawn = lambda: subprocess.Popen(  # noqa: E731
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                close_fds=True,
            )
        else:
            command = ["/bin/sh", str(script)]
            spawn = lambda: subprocess.Popen(  # noqa: E731
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
    try:
        child = spawn()
    except Exception:
        # No launcher means no apply: never leave a marker that would make the
        # next start believe an update is waiting.
        apply_marker_path(root).unlink(missing_ok=True)
        raise
    return {"started": True, "helper": str(script), "pid": child.pid, "version": to_version}


def rollback(root, force=False, busy=None):
    """Download and apply the release that was running before the last apply."""
    record = apply_state(root)
    if not record or not record.get("from_version"):
        raise ValueError("没有可以回退的上一个版本")
    previous = str(record["from_version"])
    if str(previous).lstrip("v") == str(application_version()).lstrip("v"):
        raise ValueError("当前已经运行在这个版本上")
    downloaded = download(root, tag="v" + previous.lstrip("v"))
    return apply(root, version=downloaded["version"], force=force, busy=busy)
