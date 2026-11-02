"""Build the self-contained, headless Linux Agent Harness distribution."""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path

if __package__:
    from scripts.prepare_python_runtime import validate_runtime
    from scripts.build_desktop import (
        APP_SOURCE,
        CLIENT_FILES,
        PROJECT_ROOT,
        SOURCE_ROOT,
        WEB_SOURCE,
        application_version,
    )
else:
    from prepare_python_runtime import validate_runtime
    from build_desktop import (
        APP_SOURCE,
        CLIENT_FILES,
        PROJECT_ROOT,
        SOURCE_ROOT,
        WEB_SOURCE,
        application_version,
    )


ARCHITECTURES = {
    "x86_64": "x64",
    "amd64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def _architecture():
    machine = platform.machine().lower()
    try:
        return ARCHITECTURES[machine]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported Linux architecture: {machine or 'unknown'}") from exc


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_runtime(work):
    dist = work / "pyinstaller-dist"
    build = work / "pyinstaller-build"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "harness",
        "--distpath",
        str(dist),
        "--workpath",
        str(build),
        "--specpath",
        str(work),
        "--paths",
        str(SOURCE_ROOT),
        "--add-data",
        f"{WEB_SOURCE}:web",
        "--add-data",
        f"{PROJECT_ROOT / 'VERSION'}:.",
        str(APP_SOURCE / "linux_receiver.py"),
    ]
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    runtime = dist / "harness"
    if not (runtime / "harness").is_file():
        raise RuntimeError("PyInstaller did not produce the Linux runtime")
    return runtime


def _build_service_bundle(root, python_runtime=None):
    """Add the relocatable interpreter and source bundle used by worker services."""

    python_runtime = Path(python_runtime or sys.base_prefix).resolve()
    interpreter = validate_runtime(python_runtime)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    source_stdlib = python_runtime / "lib" / version
    if not source_stdlib.is_dir():
        raise RuntimeError("Standalone Python standard library is missing")
    runtime = root / "runtime"
    (runtime / "bin").mkdir(parents=True)
    shutil.copy2(interpreter.resolve(), runtime / "bin" / "python3")
    (runtime / "bin" / "python3").chmod(0o755)
    shutil.copytree(
        source_stdlib,
        runtime / "lib" / version,
        ignore=shutil.ignore_patterns(
            "site-packages",
            "__pycache__",
            "*.pyc",
            "test",
            "tests",
            "idlelib",
            "tkinter",
            "ensurepip",
        ),
    )
    for library in (python_runtime / "lib").glob("libpython*.so*"):
        target = runtime / "lib" / library.name
        if not target.exists():
            shutil.copy2(library.resolve(), target)
    packages = runtime / "lib" / version / "site-packages"
    packages.mkdir()
    (packages / "agent_harness.pth").write_text("../../../../client\n", encoding="utf-8")
    installed = Path(sysconfig.get_path("purelib"))
    for pattern in (
        "cryptography",
        "cryptography-*.dist-info",
        "cffi",
        "cffi-*.dist-info",
        "_cffi_backend*.so",
        "pycparser",
        "pycparser-*.dist-info",
    ):
        for source in installed.glob(pattern):
            target = packages / source.name
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(source, target)
    client = root / "client" / "agent_harness"
    client.mkdir(parents=True)
    for name in ("__init__.py", *CLIENT_FILES):
        shutil.copy2(APP_SOURCE / name, client / name)
    shutil.copy2(PROJECT_ROOT / "VERSION", client / "VERSION")
    shutil.copytree(WEB_SOURCE, client / "web")


def build(output):
    if not sys.platform.startswith("linux"):
        raise RuntimeError("The Linux distribution must be built on Linux")
    version = application_version()
    architecture = _architecture()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"AgentHarness-{version}-linux-{architecture}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="agent-harness-linux-") as temporary:
        temporary = Path(temporary)
        runtime = _build_runtime(temporary)
        root = temporary / "package" / "AgentHarness-linux"
        shutil.copytree(runtime, root)
        _build_service_bundle(root)
        shutil.copy2(PROJECT_ROOT / "VERSION", root / "VERSION")
        shutil.copy2(PROJECT_ROOT / "docs/LINUX.md", root / "README.md")
        shutil.copy2(WEB_SOURCE / "linux-ai-setup.md", root / "交给AI安装.md")
        (root / "harness").chmod(0o755)
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as stream:
            stream.add(root, arcname=root.name)

    sidecar = Path(str(archive) + ".sha256")
    sidecar.write_text(f"{_digest(archive)}  {archive.name}\n", encoding="ascii")
    print(archive)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "dist")
    arguments = parser.parse_args()
    build(arguments.output_root)
