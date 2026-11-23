"""Remember one local account so the desktop can unlock it on the next start.

The password is never stored in clear text: it is wrapped by the operating system
(DPAPI on Windows, Keychain on macOS, Secret Service on Linux) and only that user's
session can unwrap it. The file names exactly one profile, so switching accounts
requires typing the password again.
"""

from pathlib import Path

from agent_harness import os_secrets
from agent_harness.common import read_json, write_json

FILE = "autologin.json"


def save(root, profile_id, password):
    """Remember one account's unlock password; False when the OS cannot protect it."""
    if not isinstance(profile_id, str) or not profile_id:
        return False
    envelope = os_secrets.protect(str(password).encode("utf-8"))
    if not envelope:
        return False
    write_json(Path(root) / FILE, {"profile_id": profile_id, "envelope": envelope})
    return True


def status(root):
    """The remembered profile id, without unwrapping anything."""
    value = read_json(Path(root) / FILE, {})
    if not isinstance(value, dict):
        return None
    profile_id = value.get("profile_id")
    return profile_id if isinstance(profile_id, str) else None


def load(root):
    """The remembered (profile_id, password), or None when absent or unreadable."""
    value = read_json(Path(root) / FILE, {})
    if not isinstance(value, dict):
        return None
    profile_id = value.get("profile_id")
    envelope = value.get("envelope")
    if not isinstance(profile_id, str) or not isinstance(envelope, dict):
        return None
    try:
        password = os_secrets.unprotect(envelope).decode("utf-8")
    except Exception:
        clear(root)
        return None
    return profile_id, password


def clear(root):
    try:
        (Path(root) / FILE).unlink()
    except OSError:
        pass
