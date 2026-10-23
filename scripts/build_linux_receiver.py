"""Package the headless receiver without a desktop or platform-specific runtime."""

import argparse
import hashlib
import tarfile
import tempfile
from pathlib import Path

from build_desktop import APP_SOURCE, PROJECT_ROOT, WEB_SOURCE, application_version


def build(output, wheelhouse=None):
    version = application_version()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"AgentHarness-{version}-linux-receiver.tar.gz"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "AgentHarness-linux"
        root.mkdir()
        import shutil

        shutil.copytree(APP_SOURCE, root / "client", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(WEB_SOURCE, root / "client/web")
        shutil.copy2(PROJECT_ROOT / "VERSION", root / "client/VERSION")
        shutil.copy2(PROJECT_ROOT / "docs/LINUX.md", root / "README.md")
        (root / "requirements.txt").write_text("cryptography==46.0.3\n", encoding="utf-8")
        if wheelhouse:
            shutil.copytree(wheelhouse, root / "wheelhouse")
        shutil.copy2(PROJECT_ROOT / "scripts/install_linux_receiver.py", root / "install.py")
        launcher = root / "harness"
        launcher.write_text(
            '#!/bin/sh\nset -eu\nroot=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec "$root/.venv/bin/python" "$root/client/linux_receiver.py" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(root, arcname=root.name)
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n", encoding="ascii"
    )
    print(archive)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "dist")
    parser.add_argument(
        "--wheelhouse", type=Path, help="Optional offline wheels for the target Python and architecture"
    )
    args = parser.parse_args()
    build(args.output_root, args.wheelhouse)
