"""Install service binaries separately from the portable UI directory."""

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from agent_harness.common import lock
from agent_harness.runtime_paths import application_home

_installed = None
MANIFEST_MAX_BYTES = 4 * 1024 * 1024


def runtime_python(bundle):
    bundle = Path(bundle)
    windows = bundle / "runtime" / "python.exe"
    return windows if windows.is_file() else bundle / "runtime" / "bin" / "python3"


def application_bundle():
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin" and executable.parent.name == "MacOS":
        return executable.parent.parent / "Resources" / "service-bundle"
    return executable.parent


def _is_link(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def bundle_manifest(bundle, inventory=None):
    """Return the digest and exact payload of the service runtime and client."""
    bundle = Path(bundle)
    files = {}
    digest = hashlib.sha256()
    for name in ("runtime", "client"):
        source = bundle / name
        if not source.is_dir() or _is_link(source):
            raise RuntimeError("应用安装不完整：缺少后台运行文件")
        paths = (
            (bundle / item for item in inventory if item.parts[0] == name)
            if inventory is not None
            else source.rglob("*")
        )
        for path in sorted(paths):
            relative = path.relative_to(bundle)
            if (
                _is_link(path)
                or inventory is not None
                and any(_is_link(bundle / parent) for parent in relative.parents if parent != Path("."))
            ):
                raise RuntimeError("后台运行文件不能是链接")
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            if path.is_file():
                with path.open("rb") as stream:
                    checksum = hashlib.file_digest(stream, "sha256").digest()
                digest.update(relative.as_posix().encode("utf-8"))
                digest.update(b"\0")
                digest.update(checksum)
                files[relative] = checksum
    if (
        runtime_python(bundle).relative_to(bundle) not in files
        or Path("client/agent_harness/desktop_service.py") not in files
    ):
        raise RuntimeError("应用安装不完整：缺少后台入口")
    return digest.hexdigest(), files


def _declared_bundle(bundle):
    manifest = Path(bundle) / "app-manifest.json"
    if not manifest.is_file():
        return None, None
    try:
        with manifest.open("rb") as stream:
            document = json.loads(stream.read(MANIFEST_MAX_BYTES + 1))
        value = document.get("service_build")
        names = document.get("service_files")
    except (OSError, ValueError, AttributeError) as exc:
        raise RuntimeError("应用安装不完整：无法读取构建清单") from exc
    if value is not None and (
        not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
    ):
        raise RuntimeError("应用安装不完整：构建摘要无效")
    if names is None:
        return value, None
    if value is None or not isinstance(names, list):
        raise RuntimeError("应用安装不完整：构建文件清单无效")
    files = set()
    for name in names:
        if not isinstance(name, str) or any(char in name for char in "\\:\x00"):
            raise RuntimeError("应用安装不完整：构建文件路径无效")
        path = Path(name)
        if len(path.parts) < 2 or path.parts[0] not in {"client", "runtime"} or ".." in path.parts or path in files:
            raise RuntimeError("应用安装不完整：构建文件路径无效")
        files.add(path)
    return value, files


def _same_bundle(target, expected, files):
    if _is_link(target) or _is_link(target / ".complete"):
        return False
    try:
        if (target / ".complete").read_text(encoding="ascii") != expected:
            return False
        return bundle_manifest(target) == (expected, files)
    except (OSError, UnicodeError, RuntimeError):
        return False


def install_bundle(bundle, destination):
    bundle, destination = Path(bundle).resolve(), Path(destination).resolve()
    expected, inventory = _declared_bundle(bundle)
    version, files = bundle_manifest(bundle, inventory)
    if expected is not None and expected != version:
        raise RuntimeError("后台运行文件校验失败：安装包内容与构建清单不一致")
    # Keep the installed runtime path short. Windows can copy files through a
    # long staging path yet later fail to import Python modules from that same
    # path. The full digest remains in .complete and is verified on every use;
    # a truncated-name collision therefore fails closed instead of reusing code.
    target = destination / version[:16]
    with lock(destination / "install.lock"):
        if target.exists() or _is_link(target):
            if _same_bundle(target, version, files):
                return target
            if expected is None:
                raise RuntimeError("后台运行文件校验失败：请从完整的新包启动应用")
            # Leave the damaged generation in place. An older worker may still
            # have files open, including on platforms that permit unlinking them.
            target = target.with_name(target.name + "-r")
            if target.exists() or _is_link(target):
                if _same_bundle(target, version, files):
                    return target
                raise RuntimeError("后台运行文件校验失败：修复副本也已损坏，请保留目录并联系维护者")
        # The staging name is deliberately short for the same reason as the
        # final content directory: nested cryptography modules can otherwise
        # cross the traditional Windows MAX_PATH boundary during installation.
        staging = Path(tempfile.mkdtemp(prefix="i-", dir=destination))
        try:
            for relative in files:
                output = staging / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundle / relative, output)
            (staging / ".complete").write_text(version, encoding="ascii")
            if not _same_bundle(staging, version, files):
                raise RuntimeError("后台运行文件校验失败：暂存文件与安装包不一致")
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return target


def installed_bundle():
    global _installed
    if _installed is None:
        home = application_home() / "service-runtime"
        _installed = install_bundle(application_bundle(), home)
    return _installed
