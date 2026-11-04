"""Runtime paths shared by the local panel and packaged application."""

import os
import shutil
import sys
from pathlib import Path


def application_home():
    """The platform's per-user storage for this application."""
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "AgentHarness"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AgentHarness"
    return Path.home() / "AgentHarness"


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


def legacy_data():
    """Where data lived before it moved beside the application."""
    return application_home() / "data"


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

    Order matters: an explicit ``AGENT_HARNESS_STATE`` wins, then an existing
    beside-the-application directory, then an existing legacy directory, and only
    for a fresh copy do we choose beside the application. An older install
    therefore keeps running from the data it already has instead of appearing
    empty after an upgrade.
    """
    override = os.environ.get("AGENT_HARNESS_STATE")
    if override:
        return Path(override)
    beside = beside_app_data()
    if beside is not None and beside.is_dir():
        return beside
    legacy = legacy_data()
    if legacy.is_dir():
        return legacy
    if beside is not None:
        return beside
    return legacy


def migrate_data(next_to, legacy=None):
    """Copy an existing legacy data directory beside the application.

    Returns the directory to use. The copy is built aside and renamed into place
    only when complete, so an interrupted run leaves the previous data intact and
    keeps using it. The legacy directory is kept as a backup, renamed so that a
    later start cannot silently go back to stale data.
    """
    import time

    destination = Path(next_to)
    source = Path(legacy or legacy_data())
    if not source.is_dir() or destination.exists():
        return destination
    if not _writable(destination.parent):
        return source
    staging = destination.with_name(destination.name + ".migrating")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(source, staging)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return source
    try:
        staging.rename(destination)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return source
    backup = source.with_name(source.name + ".migrated-" + time.strftime("%Y%m%d%H%M%S"))
    try:
        source.rename(backup)
    except OSError:
        pass
    return destination


def default_state():
    """The default ``--state``/data root, migrating legacy data when needed."""
    override = os.environ.get("AGENT_HARNESS_STATE")
    if override:
        return Path(override)
    beside = beside_app_data()
    if beside is not None and not beside.is_dir() and legacy_data().is_dir():
        return migrate_data(beside)
    return data_home()


SOURCE = Path(__file__).resolve().parent
SOURCE_LAYOUT = SOURCE.name == "agent_harness" and SOURCE.parent.name == "src"
PROJECT_ROOT = SOURCE.parents[1] if SOURCE_LAYOUT else SOURCE.parent
ASSET_ROOT = SOURCE if (SOURCE / "web").is_dir() else PROJECT_ROOT / "assets"
