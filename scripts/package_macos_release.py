"""Package a built AgentHarness.app for manual GitHub distribution."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path


def _write_sidecar(archive):
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    sidecar = Path(str(archive) + ".sha256")
    sidecar.write_text(f"{digest}  {Path(archive).name}\n", encoding="ascii")
    return sidecar


def package(app, output):
    app = Path(app).resolve()
    output = Path(output).resolve()
    if not app.is_dir() or app.suffix != ".app":
        raise ValueError("A built .app bundle is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(output)],
        check=True,
    )
    _write_sidecar(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package(args.app, args.output)
