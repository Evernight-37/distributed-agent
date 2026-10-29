"""Fetch a pinned, checksum-verified standalone Python for release builders."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path


PYTHON_VERSION = "3.12.16"
RELEASE = "20261026"
TARGETS = {
    ("Darwin", "arm64"): (
        "aarch64-apple-darwin",
        "353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e",
    ),
    ("Darwin", "x86_64"): (
        "x86_64-apple-darwin",
        "37c38fda76fec1fbf310adcbafb7f0d96af13b5511bf8ea954c3aa8eec185734",
    ),
    ("Linux", "x86_64"): (
        "x86_64-unknown-linux-gnu",
        "ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20",
    ),
    ("Linux", "arm64"): (
        "aarch64-unknown-linux-gnu",
        "bb4b6392670c5d3f93876996fcaa19858a4a413a0c7720144c18b0d6f1f0db2d",
    ),
}


def artifact(system=None, machine=None):
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    try:
        target, digest = TARGETS[system, machine]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported release runtime: {system}/{machine}") from exc
    name = f"cpython-{PYTHON_VERSION}+{RELEASE}-{target}-install_only_stripped.tar.gz"
    return name, digest


def verify_archive(path, expected):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise RuntimeError("Standalone Python archive SHA-256 mismatch")


def validate_runtime(root):
    """Reject framework Python even when it happens to work on the build host."""

    root = Path(root).resolve()
    python = root / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    if not python.is_file() or (root / "pyvenv.cfg").exists():
        raise RuntimeError("Use a standalone Python distribution, not a venv")
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import json,ssl,sqlite3,sys,sysconfig; "
            "print(json.dumps({'version':list(sys.version_info[:2]),"
            "'framework':sysconfig.get_config_var('PYTHONFRAMEWORK')}))",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    probe = json.loads(result.stdout)
    if probe.get("framework"):
        raise RuntimeError("Framework Python is not relocatable; use scripts/prepare_python_runtime.py")
    if probe.get("version") != list(sys.version_info[:2]):
        raise RuntimeError("Build Python and service runtime must have the same major/minor version")
    if Path(sys.base_prefix).resolve() != root:
        raise RuntimeError("Run the builder in a venv created by the selected standalone Python")
    return python


def prepare(destination):
    destination = Path(destination).resolve()
    name, digest = artifact()
    marker = destination / ".agent-harness-runtime.sha256"
    if destination.exists():
        if marker.is_file() and marker.read_text(encoding="ascii").strip() == digest:
            return destination
        raise RuntimeError("Runtime destination already exists without the expected checksum marker")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="python-runtime-", dir=destination.parent) as folder:
        staging = Path(folder)
        archive = staging / name
        url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{RELEASE}/{name}"
        with urllib.request.urlopen(url, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        verify_archive(archive, digest)
        with tarfile.open(archive) as stream:
            stream.extractall(staging / "unpacked", filter="data")
        root = staging / "unpacked" / "python"
        if not (root / "bin" / "python3").is_file():
            raise RuntimeError("Standalone Python archive has no interpreter")
        (root / marker.name).write_text(digest + "\n", encoding="ascii")
        root.rename(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.destination))
