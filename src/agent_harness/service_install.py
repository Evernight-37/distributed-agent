"""Install service binaries separately from the portable UI directory."""

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path
from agent_harness.common import lock
from agent_harness.runtime_paths import application_home

_installed = None


def runtime_python(bundle):
    bundle = Path(bundle)
    windows = bundle / "runtime" / "python.exe"
    return windows if windows.is_file() else bundle / "runtime" / "bin" / "python3"


def application_bundle():
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin" and executable.parent.name == "MacOS":
        return executable.parent.parent / "Resources" / "service-bundle"
    return executable.parent


def install_bundle(bundle, destination):
    bundle, destination = Path(bundle).resolve(), Path(destination).resolve()
    files = []
    digest = hashlib.sha256()
    for name in ("runtime", "client"):
        source = bundle / name
        if not source.is_dir():
            raise RuntimeError("应用安装不完整：缺少后台运行文件")
        for path in sorted(source.rglob("*")):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise RuntimeError("后台运行文件不能是链接")
            if path.is_file():
                relative = path.relative_to(bundle)
                digest.update(str(relative).replace("\\", "/").encode("utf-8"))
                digest.update(b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
                files.append((path, relative))
    package_entry = bundle / "client/agent_harness/desktop_service.py"
    legacy_entry = bundle / "client/desktop_service.py"
    if not runtime_python(bundle).is_file() or not (package_entry.is_file() or legacy_entry.is_file()):
        raise RuntimeError("应用安装不完整：缺少后台入口")
    version = digest.hexdigest()
    # Keep the installed runtime path short. Windows can copy files through a
    # long staging path yet later fail to import Python modules from that same
    # path. The full digest remains in .complete and is verified on every use;
    # a truncated-name collision therefore fails closed instead of reusing code.
    target = destination / version[:16]
    with lock(destination / "install.lock"):
        if (target / ".complete").is_file():
            if (target / ".complete").read_text(encoding="ascii") != version:
                raise RuntimeError("后台运行文件校验失败：版本标记不一致")
            for source, relative in files:
                deployed = target / relative
                if (
                    not deployed.is_file()
                    or hashlib.sha256(deployed.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest()
                ):
                    raise RuntimeError("后台运行文件校验失败：请从完整的新包启动应用")
            return target
        if target.exists():
            raise RuntimeError("后台运行目录安装不完整，请保留目录并联系维护者")
        # The staging name is deliberately short for the same reason as the
        # final content directory: nested cryptography modules can otherwise
        # cross the traditional Windows MAX_PATH boundary during installation.
        staging = Path(tempfile.mkdtemp(prefix="i-", dir=destination))
        try:
            for source, relative in files:
                output = staging / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, output)
            (staging / ".complete").write_text(version, encoding="ascii")
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
