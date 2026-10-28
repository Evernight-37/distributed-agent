"""Build a macOS app with an independent, relocatable Python service runtime."""

import argparse
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from build_desktop import (
    APP_SOURCE,
    CLIENT_FILES,
    PROJECT_ROOT,
    SOURCE_ROOT,
    WEB_SOURCE,
    application_version,
)


def build(output_root, python_runtime):
    if sys.platform != "darwin":
        raise RuntimeError("Build this app on macOS")
    version = application_version()
    output_root = Path(output_root).resolve()
    python_runtime = Path(python_runtime).resolve()
    runtime_python = python_runtime / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    if not runtime_python.is_file() or (python_runtime / "pyvenv.cfg").exists():
        raise RuntimeError("--python-runtime must be a relocatable standalone Python distribution, not a venv")
    # Some standalone distributions load bundled trust-policy PEM files from
    # beside ssl.py. Frozen ssl.__file__ points inside _MEIPASS, so these files
    # must also be present in the frontend, not only in the service runtime.
    trust_data = []
    for certificate in sorted(Path(sysconfig.get_path("stdlib")).glob("*.pem")):
        trust_data.extend(["--add-data", f"{certificate}:."])
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--windowed",
            "--onedir",
            "--distpath",
            str(output_root),
            "--workpath",
            str(PROJECT_ROOT / "build" / "macos"),
            "--specpath",
            str(PROJECT_ROOT / "build"),
            "--paths",
            str(SOURCE_ROOT),
            *trust_data,
            "--hidden-import",
            "webview.platforms.cocoa",
            "--collect-all",
            "webview",
            "--collect-all",
            "cryptography",
            "--exclude-module",
            "webview.platforms.qt",
            "--exclude-module",
            "webview.platforms.gtk",
            "--exclude-module",
            "webview.platforms.winforms",
            "--add-data",
            f"{WEB_SOURCE}:web",
            "--name",
            "AgentHarness",
            "--osx-bundle-identifier",
            "org.agentharness.desktop",
            str(APP_SOURCE / "production_app.py"),
        ],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "PYINSTALLER_CONFIG_DIR": str(PROJECT_ROOT / "build" / "pyinstaller-cache"),
        },
        check=True,
    )
    app = output_root / "AgentHarness.app"
    contents = app / "Contents"
    bundle = contents / "Resources" / "service-bundle"
    runtime = bundle / "runtime"
    stdlib_name = f"python{sys.version_info.major}.{sys.version_info.minor}"
    (runtime / "bin").mkdir(parents=True)
    shutil.copy2(runtime_python, runtime / "bin" / "python3")
    # Keep only the interpreter, standard library and service dependencies.
    shutil.copytree(
        python_runtime / "lib" / stdlib_name,
        runtime / "lib" / stdlib_name,
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
    for library in (python_runtime / "lib").glob("libpython*.dylib"):
        shutil.copy2(library, runtime / "lib" / library.name, follow_symlinks=True)
    packages = runtime / "lib" / stdlib_name / "site-packages"
    packages.mkdir()
    # The relocatable service interpreter imports the versioned application
    # package without relying on a shell working directory or user PYTHONPATH.
    (packages / "agent_harness.pth").write_text("../../../../client\n", encoding="utf-8")
    installed_packages = Path(sysconfig.get_path("purelib"))
    for pattern in (
        "cryptography",
        "cryptography-*.dist-info",
        "cffi",
        "cffi-*.dist-info",
        "_cffi_backend*.so",
        "pycparser",
        "pycparser-*.dist-info",
    ):
        for source in installed_packages.glob(pattern):
            target = packages / source.name
            if source.is_dir():
                shutil.copytree(
                    source,
                    target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            else:
                shutil.copy2(source, target)
    client = bundle / "client"
    package = client / "agent_harness"
    package.mkdir(parents=True)
    for name in ("__init__.py", *CLIENT_FILES):
        shutil.copy2(APP_SOURCE / name, package / name)
    shutil.copy2(PROJECT_ROOT / "VERSION", package / "VERSION")
    shutil.copytree(WEB_SOURCE, package / "web")
    manifest = {
        "format": 1,
        "name": "AgentHarness",
        "version": version,
        "platform": "macos",
        "architecture": platform.machine(),
        "python_runtime": platform.python_version(),
    }
    (bundle / "app-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (bundle / "VERSION").write_text(version + "\n", encoding="utf-8")
    plist_path = contents / "Info.plist"
    with plist_path.open("rb") as stream:
        info = plistlib.load(stream)
    info.update(
        CFBundleShortVersionString=version,
        CFBundleVersion=version,
        NSHighResolutionCapable=True,
    )
    with plist_path.open("wb") as stream:
        plistlib.dump(info, stream)
    subprocess.run(
        [
            str(runtime / "bin" / "python3"),
            "-B",
            "-I",
            "-c",
            "import ssl,sqlite3; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; "
            "k=Ed25519PrivateKey.generate(); k.public_key().verify(k.sign(b'check'),b'check'); print('Portable runtime verified')",
        ],
        cwd=output_root,
        check=True,
    )
    # Cloud-backed Desktop folders may recreate FinderInfo while codesign runs.
    # Sign on a local temporary volume, then copy the signed bundle back.
    with tempfile.TemporaryDirectory(prefix="agentharness-sign-", dir="/private/tmp") as temporary:
        staged = Path(temporary) / app.name
        subprocess.run(["ditto", "--norsrc", "--noextattr", str(app), str(staged)], check=True)
        subprocess.run(["xattr", "-cr", str(staged)], check=True)
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(staged)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(staged)], check=True)
        shutil.rmtree(app)
        subprocess.run(["ditto", "--norsrc", "--noextattr", str(staged), str(app)], check=True)
    print(app)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "dist" / "macos")
    parser.add_argument("--python-runtime", type=Path, default=Path(sys.base_prefix))
    arguments = parser.parse_args()
    build(arguments.output_root, arguments.python_runtime)
