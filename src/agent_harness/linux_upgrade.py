"""Verified, side-by-side upgrades for the headless Linux receiver."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

from agent_harness.common import lock, read_json, write_json
from agent_harness.linux_service import ServiceManager


VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
MAX_ARCHIVE_MEMBERS = 4096
MAX_ARCHIVE_FILE_SIZE = 256 * 1024 * 1024
MAX_ARCHIVE_TOTAL_SIZE = 1024 * 1024 * 1024


def _digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _members(archive):
    members = []
    roots = set()
    paths = set()
    total_size = 0
    for member in archive:
        if len(members) >= MAX_ARCHIVE_MEMBERS:
            raise ValueError("升级包文件数量过多")
        members.append(member)
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or not name.parts:
            raise ValueError("升级包包含不安全路径")
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise ValueError("升级包不能包含链接或设备文件")
        if not (member.isdir() or member.isfile()):
            raise ValueError("升级包包含不支持的文件类型")
        normalized = name.as_posix().rstrip("/")
        if normalized in paths:
            raise ValueError("升级包包含重复路径")
        paths.add(normalized)
        if member.isfile():
            if member.size < 0 or member.size > MAX_ARCHIVE_FILE_SIZE:
                raise ValueError("升级包包含过大的单个文件")
            total_size += member.size
            if total_size > MAX_ARCHIVE_TOTAL_SIZE:
                raise ValueError("升级包解压后总体积过大")
        roots.add(name.parts[0])
    if len(roots) != 1:
        raise ValueError("升级包必须只有一个顶层目录")
    return members, roots.pop()


def _extract(bundle, destination):
    with tarfile.open(bundle, "r:gz") as archive:
        members, top = _members(archive)
        base = Path(destination).resolve()
        extracted_size = 0
        for member in members:
            relative = PurePosixPath(member.name)
            target = base.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(base):
                raise ValueError("升级包路径越界")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("升级包文件无法读取")
            with source, target.open("wb") as output:
                written = 0
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    written += len(chunk)
                    extracted_size += len(chunk)
                    if written > MAX_ARCHIVE_FILE_SIZE or extracted_size > MAX_ARCHIVE_TOTAL_SIZE:
                        raise ValueError("升级包实际解压体积超过限制")
                    output.write(chunk)
                if written != member.size:
                    raise ValueError("升级包文件大小与清单不一致")
            target.chmod(member.mode & 0o777)
        return base / top


def active_jobs(root):
    jobs = []
    for receipt in (Path(root) / "receivers").glob("device-*/pull-receipts/*.json"):
        value = read_json(receipt, {})
        if value.get("phase") not in {None, "terminal"}:
            jobs.append(receipt.name)
    return jobs


@contextmanager
def quiesced_receivers(root):
    """Prevent a receiver from committing a new local job during a switch."""
    receivers = sorted((Path(root) / "receivers").glob("device-*"))
    with ExitStack() as stack:
        for receiver in receivers:
            if receiver.is_dir():
                stack.enter_context(lock(receiver / "pull-receipts.lock"))
        jobs = active_jobs(root)
        if jobs:
            raise RuntimeError(f"仍有 {len(jobs)} 个未结束任务；请等待完成后升级")
        yield


def _record_path(root):
    return Path(root).resolve().parent / "install-state.json"


def _runtime(release):
    release = Path(release).resolve()
    executable = release / "harness"
    if executable.is_file():
        return executable, None
    python = release / ".venv" / "bin" / "python"
    script = release / "client" / "linux_receiver.py"
    if not python.is_file() or not script.is_file():
        raise ValueError("Linux 接收端安装不完整")
    return python, script


def _current_runtime(root):
    """Return the active recorded runtime, falling back only before state exists."""
    record = read_json(_record_path(root), {})
    current = record.get("current") if isinstance(record, dict) else None
    if current is not None:
        if (
            not isinstance(current, dict)
            or not isinstance(current.get("release"), str)
            or not isinstance(current.get("python"), str)
        ):
            raise ValueError("Linux 安装状态中的当前版本无效")
        release = Path(current["release"]).expanduser().resolve()
        python = Path(current["python"]).expanduser().resolve()
        script_value = current.get("script")
        script = Path(script_value).expanduser().resolve() if isinstance(script_value, str) and script_value else None
        if not python.is_file() or (script is not None and not script.is_file()):
            raise ValueError("Linux 安装状态中的运行路径无效")
        return release, python, script
    if getattr(sys, "frozen", False):
        return (
            Path(sys.executable).resolve().parent,
            Path(sys.executable).resolve(),
            None,
        )
    return (
        Path(__file__).resolve().parent.parent,
        Path(sys.executable).resolve(),
        Path(__file__).resolve().parent / "linux_receiver.py",
    )


def _bundle_marker(release):
    return Path(release) / ".agent-harness-bundle-sha256"


def _write_bundle_marker(release, digest):
    marker = _bundle_marker(release)
    marker.write_text(digest + "\n", encoding="ascii")
    marker.chmod(0o600)


def _refresh_launchers(refresh_launchers, root, executable, script):
    refresh_launchers(
        root,
        python=executable,
        adapter=script.parent / "mcp_adapter.py" if script is not None else None,
        standalone=script is None,
    )


def _record_runtime(release, executable, script):
    return {
        "release": str(release),
        "python": str(executable),
        "script": str(script) if script is not None else None,
        "kind": "standalone" if script is None else "python",
    }


def _runtime_from_record(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("release"), str)
        or not isinstance(value.get("python"), str)
    ):
        raise ValueError("Linux 安装状态无效")
    release = Path(value["release"]).expanduser().resolve()
    executable = Path(value["python"]).expanduser().resolve()
    script_value = value.get("script")
    script = Path(script_value).expanduser().resolve() if isinstance(script_value, str) and script_value else None
    if not executable.is_file() or (script is not None and not script.is_file()):
        raise ValueError("Linux 安装状态中的版本文件已不存在")
    return release, executable, script


def _prune_releases(releases, keep, limit=3):
    """Keep current/previous plus the newest rollback candidates."""

    releases = Path(releases).resolve()
    keep = {Path(item).resolve() for item in keep}
    candidates = sorted(
        (
            path
            for path in releases.iterdir()
            if path.is_dir() and VERSION_PATTERN.fullmatch(path.name) and path.resolve().parent == releases
        ),
        key=lambda path: (
            path.stat().st_mtime,
            tuple(int(part) for part in path.name.split(".")),
        ),
        reverse=True,
    )
    retained = {path for path in keep if path.parent == releases}
    for path in candidates:
        if len(retained) >= limit:
            break
        retained.add(path.resolve())
    for path in candidates:
        if path.resolve() not in retained:
            shutil.rmtree(path)


def upgrade(root, bundle, expected_sha256, refresh_launchers, retry=False):
    root = Path(root).resolve()
    bundle = Path(bundle).expanduser().resolve()
    expected = expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("SHA-256 必须是 64 位十六进制摘要")
    if _digest(bundle) != expected:
        raise ValueError("升级包 SHA-256 校验失败")
    jobs = active_jobs(root)
    if jobs:
        raise RuntimeError(f"仍有 {len(jobs)} 个未结束任务；请等待完成后升级")
    releases = root.parent / "releases"
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    releases.chmod(0o700)
    old_release, old_python, old_script = _current_runtime(root)
    reused = False
    with tempfile.TemporaryDirectory(dir=releases) as temporary:
        unpacked = _extract(bundle, temporary)
        version_file = unpacked / "VERSION"
        if not version_file.is_file():
            version_file = unpacked / "client" / "VERSION"
        if not version_file.is_file():
            raise ValueError("升级包缺少 VERSION")
        version = version_file.read_text(encoding="utf-8").strip()
        if not VERSION_PATTERN.fullmatch(version):
            raise ValueError("升级包版本无效")
        destination = releases / version
        if destination.exists():
            if not retry:
                raise ValueError("该版本已安装；确认是上次失败后可使用 --retry 重新激活")
            marker = _bundle_marker(destination)
            if not marker.is_file() or marker.read_text(encoding="ascii").strip() != expected:
                raise ValueError("已安装版本与本次升级包无法安全匹配")
            _runtime(destination)
            reused = True
        else:
            os.replace(unpacked, destination)
    if not reused:
        try:
            installer = destination / "install.py"
            if installer.is_file():
                # Legacy 1.1 bundles embed absolute venv paths and must be
                # installed only after reaching their final location.
                subprocess.run([sys.executable, str(installer)], cwd=destination, check=True)
            _runtime(destination)
            _write_bundle_marker(destination, expected)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            cleanup_error = None
            try:
                shutil.rmtree(destination)
            except OSError as cleanup_exc:
                cleanup_error = cleanup_exc
            message = "新版本准备失败；当前服务未切换"
            if cleanup_error is not None:
                message += f"；失败目录清理也未完成（{type(cleanup_error).__name__}）"
            raise RuntimeError(message) from exc
    new_python, new_script = _runtime(destination)
    new_manager = ServiceManager(root, new_python, new_script)
    old_manager = ServiceManager(root, old_python, old_script)
    try:
        with quiesced_receivers(root):
            _refresh_launchers(refresh_launchers, root, new_python, new_script)
            new_manager.install()
    except Exception:
        _refresh_launchers(refresh_launchers, root, old_python, old_script)
        old_manager.install()
        raise
    record = {
        "current": _record_runtime(destination, new_python, new_script),
        "previous": _record_runtime(old_release, old_python, old_script),
    }
    path = _record_path(root)
    write_json(path, record)
    path.chmod(0o600)
    try:
        _prune_releases(releases, {destination, old_release})
    except OSError:
        # A stale release held by another process is harmless. A later
        # successful upgrade will retry retention without blocking service.
        pass
    return {
        "version": version,
        "release": str(destination),
        "rollback_available": True,
        "reused": reused,
    }


def rollback(root, refresh_launchers):
    root = Path(root).resolve()
    path = _record_path(root)
    record = read_json(path, {})
    previous = record.get("previous") if isinstance(record, dict) else None
    current = record.get("current") if isinstance(record, dict) else None
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ValueError("没有可回滚的 Linux 接收端版本")
    try:
        _, python, script = _runtime_from_record(previous)
    except ValueError as exc:
        raise ValueError("上一版本文件已不存在，无法回滚") from exc
    if active_jobs(root):
        raise RuntimeError("仍有未结束任务；请等待完成后回滚")
    try:
        _, current_python, current_script = _runtime_from_record(current)
    except ValueError:
        current_python = current_script = None
    try:
        with quiesced_receivers(root):
            _refresh_launchers(refresh_launchers, root, python, script)
            ServiceManager(root, python, script).install()
    except Exception:
        if current_python is not None:
            _refresh_launchers(refresh_launchers, root, current_python, current_script)
            ServiceManager(root, current_python, current_script).install()
        raise
    write_json(path, {"current": previous, "previous": current})
    path.chmod(0o600)
    return {"release": previous.get("release"), "rollback_available": True}
