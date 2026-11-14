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


def has_profiles(directory):
    """Whether an account is stored under this data directory."""
    try:
        return any((Path(directory) / "profiles").glob("*.json"))
    except OSError:
        return False


def data_home():
    """Resolve the data directory.

    ``FOOLSHIP_STATE`` wins. Otherwise data lives beside a portable or installed
    copy, and macOS keeps the platform location because a .app bundle is sealed.

    A copy in a directory that is *not* writable falls back to the platform
    location. That fallback must also apply the other way round: a fresh copy in
    a writable directory must not hide the account that already lives in the
    platform directory, or moving or re-extracting the app looks like the account
    disappeared.
    """
    override = os.environ.get("FOOLSHIP_STATE")
    if override:
        return Path(override)
    beside = beside_app_data()
    platform = application_home() / "data"
    if beside is not None and has_profiles(beside):
        return beside
    if has_profiles(platform):
        return platform
    if beside is not None:
        return beside
    return platform


def default_state():
    """The default ``--state``/data root."""
    return data_home()


SOURCE = Path(__file__).resolve().parent
SOURCE_LAYOUT = SOURCE.name == "agent_harness" and SOURCE.parent.name == "src"
PROJECT_ROOT = SOURCE.parents[1] if SOURCE_LAYOUT else SOURCE.parent
ASSET_ROOT = SOURCE if (SOURCE / "web").is_dir() else PROJECT_ROOT / "assets"
