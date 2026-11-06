"""Which senders this machine has already accepted a file from.

Kept locally, next to the receive folder: it is the receiving machine's own
security policy, not something the collaboration service needs to know. A first
transfer from an unfamiliar account always asks; after the owner accepts, later
files from that account may go straight to the default folder.
"""

import time
from pathlib import Path

from agent_harness.common import lock, read_json, write_json

LIMIT = 200


def _path(root):
    return Path(root) / "transfer-trust.json"


def _document(root):
    try:
        value = read_json(_path(root), {"version": 1, "senders": []})
    except (ValueError, OSError):
        return {"version": 1, "senders": []}
    if not isinstance(value, dict) or not isinstance(value.get("senders"), list):
        return {"version": 1, "senders": []}
    return value


def senders(root):
    return [
        entry
        for entry in _document(root)["senders"]
        if isinstance(entry, dict) and isinstance(entry.get("account"), str) and entry["account"]
    ]


def is_trusted(root, account):
    return any(entry["account"] == account for entry in senders(root))


def remember(root, account):
    """Record that the owner accepted a file from this account."""
    if not isinstance(account, str) or not account:
        raise ValueError("Invalid sender account")
    path = _path(root)
    with lock(Path(root) / "transfer-trust.lock"):
        entries = [entry for entry in _document(root)["senders"] if isinstance(entry, dict)]
        entries = [entry for entry in entries if entry.get("account") != account]
        entries.append({"account": account, "accepted_at": time.time()})
        write_json(path, {"version": 1, "senders": entries[-LIMIT:]})
    return account


def forget(root, account):
    path = _path(root)
    with lock(Path(root) / "transfer-trust.lock"):
        entries = [entry for entry in _document(root)["senders"] if isinstance(entry, dict)]
        entries = [entry for entry in entries if entry.get("account") != account]
        write_json(path, {"version": 1, "senders": entries})
