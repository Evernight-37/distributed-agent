"""Release metadata available in both source and installed runtime layouts."""

from pathlib import Path


def version_path():
    source = Path(__file__).resolve()
    for candidate in (source.with_name("VERSION"), source.parents[2] / "VERSION"):
        if candidate.is_file():
            return candidate
    return None


def version():
    path = version_path()
    if not path:
        return "unknown"
    value = path.read_text(encoding="utf-8").strip()
    return value or "unknown"
