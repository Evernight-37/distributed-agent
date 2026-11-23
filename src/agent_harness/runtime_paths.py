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


def migrate_beside_data(source, destination):
    """Copy an installation-local data directory to the platform location.

    Older builds kept data beside the executable, which made moving or copying the
    program look like the account disappeared. The program is now data-free and the
    data lives at a stable machine location; the first start copies it there and
    renames the old directory rather than deleting anything. A failure leaves the
    old directory in use.
    """
    import shutil
    import time

    source, destination = Path(source), Path(destination)
    if not source.is_dir() or has_profiles(destination):
        return False
    # A receiver or host service started from this directory would keep running
    # from the renamed copy and compete with the migrated one.
    try:
        from agent_harness.app_lifecycle import Lifecycle

        Lifecycle(source).stop()
    except Exception:
        pass
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.lock"))
    except OSError:
        return False
    if not has_profiles(destination):
        return False
    try:
        source.rename(source.with_name("data.migrated-" + time.strftime("%Y%m%d-%H%M%S")))
    except OSError:
        # The data is already copied and in use; leaving the old copy alone is
        # better than failing the start.
        pass
    return True


def data_home():
    """Resolve the data directory.

    ``FOOLSHIP_STATE`` wins. Otherwise every platform uses its own per-user
    location: the program directory belongs to the program alone, so copying,
    moving or reinstalling it never touches accounts, the team database or
    received files.

    A one-time migration handles installations from the era when data lived beside
    the executable.
    """
    override = os.environ.get("FOOLSHIP_STATE")
    if override:
        return Path(override)
    platform = application_home() / "data"
    if has_profiles(platform):
        return platform
    beside = beside_app_data()
    if beside is not None and has_profiles(beside):
        if migrate_beside_data(beside, platform):
            return platform
        # Migration failed: keep working from the old location rather than
        # starting empty.
        return beside
    return platform


def default_state():
    """The default ``--state``/data root."""
    return data_home()


SOURCE = Path(__file__).resolve().parent
SOURCE_LAYOUT = SOURCE.name == "agent_harness" and SOURCE.parent.name == "src"
PROJECT_ROOT = SOURCE.parents[1] if SOURCE_LAYOUT else SOURCE.parent
ASSET_ROOT = SOURCE if (SOURCE / "web").is_dir() else PROJECT_ROOT / "assets"


def documents_home():
    """The user's Documents folder, following platform redirection (OneDrive)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            buffer = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_PERSONAL = 5, SHGFP_TYPE_CURRENT = 0: the folder a user sees.
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0 and buffer.value:
                return Path(buffer.value)
        except Exception:
            pass
    return Path.home() / "Documents"


def received_files_home(data_root=None):
    """Where received files land when the user never chose a folder.

    Received files are user content, not application state, so they belong in a
    place people already look at: ``Documents/Foolship/Received``. The data
    directory is the fallback for a machine where Documents cannot be written.
    """
    candidate = documents_home() / "Foolship" / "Received"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        if _writable(candidate):
            return candidate
    except OSError:
        pass
    fallback = (Path(data_root) if data_root else application_home() / "data") / "received-files"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return fallback
