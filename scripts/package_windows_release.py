"""Create a deterministic, UTF-8-safe Windows release archive."""

from __future__ import annotations

import argparse
import os
import time
import zipfile
from pathlib import Path


def _archive_timestamp() -> tuple[int, int, int, int, int, int]:
    """Return a ZIP-compatible timestamp, honoring reproducible-build input."""
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "315532800"))
    return tuple(time.gmtime(max(epoch, 315532800))[:6])


def package_release(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if not source.is_dir():
        raise ValueError(f"release source is not a directory: {source}")
    for required in ("AgentHarness.exe", "VERSION", "app-manifest.json"):
        if not (source / required).is_file():
            raise ValueError(f"release source is missing {required}")
    if destination == source or source in destination.parents:
        raise ValueError("archive destination must be outside the release tree")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)
    timestamp = _archive_timestamp()
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"release tree contains a symbolic link: {path}")
                if not path.is_file():
                    continue
                relative = path.relative_to(source)
                name = Path("AgentHarness", relative).as_posix()
                info = zipfile.ZipInfo(name, timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes(), compresslevel=9)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    package_release(args.source, args.destination)


if __name__ == "__main__":
    main()
