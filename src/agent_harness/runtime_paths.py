"""Runtime paths shared by the local panel and packaged application."""

import os
import sys
from pathlib import Path


def application_home():
    """The platform's per-user storage for this application."""
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Foolship"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Foolship"
    return Path.home() / "Foolship"


def bundled_root():
    """The directory holding the application binaries, or None when run from source."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def installation_root():
    """Where files belonging to the installation may be written.

    On Windows everything the application owns lives in the directory it was
    unpacked into. A macOS .app is a sealed bundle, so derived files go to the
    platform location there instead.
    """
    if sys.platform == "darwin":
        return application_home()
    root = bundled_root()
    return root if root is not None else application_home()


def _writable(directory):
    """Whether data could actually be written here, without creating anything."""
    probe = Path(directory)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return os.access(probe, os.W_OK)
    except OSError:
        return False


def beside_app_data():
    """Data beside a portable or installed copy, or None when that makes no sense."""
    if sys.platform == "darwin":
        # A .app is a sealed bundle: writing user data inside it is wrong and may
        # be impossible. macOS keeps the platform location.
        return None
    root = bundled_root()
    if root is None:
        # Running from source: never scatter data into the checkout.
        return None
    if not _writable(root):
        return None
    return root / "data"


def data_home():
    """Resolve the data directory.

    ``FOOLSHIP_STATE`` wins; otherwise a portable or installed copy keeps its
    data beside the application, and macOS (a sealed .app bundle) uses the
    platform location.
    """
    override = os.environ.get("FOOLSHIP_STATE")
    if override:
        return Path(override)
    beside = beside_app_data()
    if beside is not None:
        return beside
    return application_home() / "data"


def default_state():
    """The default ``--state``/data root."""
    return data_home()


SOURCE = Path(__file__).resolve().parent
SOURCE_LAYOUT = SOURCE.name == "agent_harness" and SOURCE.parent.name == "src"
PROJECT_ROOT = SOURCE.parents[1] if SOURCE_LAYOUT else SOURCE.parent
ASSET_ROOT = SOURCE if (SOURCE / "web").is_dir() else PROJECT_ROOT / "assets"
