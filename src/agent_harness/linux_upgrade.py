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

from common import lock, read_json, write_json
from linux_service import ServiceManager


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
        if not isinstance(current, dict) or not all(
            isinstance(current.get(key), str) for key in ("release", "python", "script")
        ):
            raise ValueError("Linux 安装状态中的当前版本无效")
        release = Path(current["release"]).expanduser().resolve()
        python, script = _runtime(release)
        recorded_python = Path(current["python"]).expanduser().absolute()
        recorded_script = Path(current["script"]).expanduser().absolute()
        if python != recorded_python or script != recorded_script:
            raise ValueError("Linux 安装状态中的运行路径不匹配")
        return release, python, script
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
            # Virtual environments embed absolute interpreter paths and must be
            # created only after the release reaches its final location.
            subprocess.run([sys.executable, str(destination / "install.py")], cwd=destination, check=True)
            _write_bundle_marker(destination, expected)
        except (OSError, subprocess.CalledProcessError) as exc:
            cleanup_error = None
            try:
                shutil.rmtree(destination)
            except OSError as cleanup_exc:
                cleanup_error = cleanup_exc
            message = "新版本依赖安装失败；当前服务未切换"
            if cleanup_error is not None:
                message += f"；失败目录清理也未完成（{type(cleanup_error).__name__}）"
            raise RuntimeError(message) from exc
    new_python, new_script = _runtime(destination)
    new_manager = ServiceManager(root, new_python, new_script)
    old_manager = ServiceManager(root, old_python, old_script)
    try:
        with quiesced_receivers(root):
            refresh_launchers(root, python=new_python, adapter=new_script.parent / "mcp_adapter.py")
            new_manager.install()
    except Exception:
        refresh_launchers(root, python=old_python, adapter=old_script.parent / "mcp_adapter.py")
        old_manager.install()
        raise
    record = {
        "current": {"release": str(destination), "python": str(new_python), "script": str(new_script)},
        "previous": {"release": str(old_release), "python": str(old_python), "script": str(old_script)},
    }
    path = _record_path(root)
    write_json(path, record)
    path.chmod(0o600)
    return {"version": version, "release": str(destination), "rollback_available": True, "reused": reused}


def rollback(root, refresh_launchers):
    root = Path(root).resolve()
    path = _record_path(root)
    record = read_json(path, {})
    previous = record.get("previous") if isinstance(record, dict) else None
    current = record.get("current") if isinstance(record, dict) else None
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ValueError("没有可回滚的 Linux 接收端版本")
    python, script = Path(previous.get("python", "")), Path(previous.get("script", ""))
    if not python.is_file() or not script.is_file():
        raise ValueError("上一版本文件已不存在，无法回滚")
    if active_jobs(root):
        raise RuntimeError("仍有未结束任务；请等待完成后回滚")
    current_python = Path(current.get("python", ""))
    current_script = Path(current.get("script", ""))
    try:
        with quiesced_receivers(root):
            refresh_launchers(root, python=python, adapter=script.parent / "mcp_adapter.py")
            ServiceManager(root, python, script).install()
    except Exception:
        if current_python.is_file() and current_script.is_file():
            refresh_launchers(root, python=current_python, adapter=current_script.parent / "mcp_adapter.py")
            ServiceManager(root, current_python, current_script).install()
        raise
    write_json(path, {"current": previous, "previous": current})
    path.chmod(0o600)
    return {"release": previous.get("release"), "rollback_available": True}
