"""Release metadata available in both source and installed runtime layouts."""

from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path


def version_path():
    source = Path(__file__).resolve()
    for candidate in (source.with_name("VERSION"), source.parents[2] / "VERSION"):
        if candidate.is_file():
            return candidate
    return None


def version():
    path = version_path()
    if path:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    try:
        return package_version("agent-harness")
    except PackageNotFoundError:
        return "unknown"


__version__ = version()
