"""Offline clean-room verification for one unpacked Windows or macOS release."""

import argparse
import json
import os
import py_compile
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("scratch", type=Path)
    args = parser.parse_args()
    package = args.package.resolve()
    app = package if package.suffix == ".app" else None
    if app:
        package = app / "Contents" / "Resources" / "service-bundle"
    scratch = args.scratch.resolve()
    expected_version = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    local = scratch / "localappdata"
    local.mkdir()
    os.environ["LOCALAPPDATA"] = str(local)
    os.environ["CODEX_HOME"] = str(scratch / "codex-home")
    client = package / "client"
    client_package = client / "agent_harness"
    runtime = package / "runtime" / ("bin/python3" if app else "python.exe")
    required = (
        app / "Contents/MacOS/AgentHarness" if app else package / "AgentHarness.exe",
        runtime,
        client_package / "app_bridge.py",
        client_package / "mcp_adapter.py",
        package / "VERSION",
    )
    if not all(path.is_file() for path in required):
        raise RuntimeError("release is incomplete")
    if (package / "VERSION").read_text(encoding="utf-8").strip() != expected_version:
        raise RuntimeError("wrong release version")
    compiled = scratch / "compiled"
    compiled.mkdir()
    for source in client_package.glob("*.py"):
        py_compile.compile(str(source), cfile=str(compiled / (source.name + "c")), doraise=True)
    probe = Path(__file__).with_name("cleanroom_runtime_probe.py")
    result = subprocess.run(
        [
            str(runtime),
            "-X",
            "utf8",
            str(probe),
            str(package),
            str(scratch),
            expected_version,
            str(Path(__file__).resolve().parents[1]),
            *(["--macos"] if app else []),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=40,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode:
        raise RuntimeError("packaged runtime probe failed: " + result.stderr[-1000:])
    report = json.loads(result.stdout.strip())
    if report.get("version") != expected_version or report.get("compiled_modules") != len(
        list(client_package.glob("*.py"))
    ):
        raise RuntimeError("packaged runtime probe returned inconsistent metadata")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
