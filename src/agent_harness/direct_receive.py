"""Receiving direct transfers in the desktop app.

The bytes never touch the service: this module learns where a sender is listening,
proves it is the addressed device with a signature, and pulls the file straight
from that address. The service only ever holds the offer and the progress.
"""

import os
import threading
import time
from pathlib import Path

from agent_harness import direct_transfer, transfer_trust
from agent_harness.attachments import safe_name
from agent_harness.common import read_json, write_json

INCOMING_STATES = {"queued", "accepted", "running", "done", "failed", "rejected"}

_LOCK = threading.RLock()
_KEYS = {}
_INFLIGHT = set()
_PUBLISHED = set()
_PROGRESS_AT = {}
_WATCH = {"thread": None}
WATCH_SECONDS = 4.0


def _keypair(root):
    """This desktop's own signing identity for direct transfers."""
    path = Path(root) / "transfer-key.json"
    key = str(path)
    with _LOCK:
        cached = _KEYS.get(key)
        if cached:
            return cached
        stored = read_json(path, {})
        if isinstance(stored, dict) and stored.get("private_key") and stored.get("public_key"):
            _KEYS[key] = stored
            return stored
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

        pair = Ed25519PrivateKey.generate()
        value = {
            "private_key": base64.b64encode(pair.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode(
                "ascii"
            ),
            "public_key": base64.b64encode(pair.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(
                "ascii"
            ),
        }
        write_json(path, value)
        if os.name != "nt":
            try:
                path.chmod(0o600)
            except OSError:
                pass
        _KEYS[key] = value
        return value


def public_key(bridge):
    return _keypair(bridge.root)["public_key"]


def publish(bridge, server):
    """Tell the service which key this desktop would sign a download with."""
    key = public_key(bridge)
    marker = (server, bridge.profile.get("node"), key)
    if marker in _PUBLISHED:
        return
    bridge._rpc("transfer-key-publish", {"transfer_public_key": key}, server=server)
    _PUBLISHED.add(marker)


def receive_folder(bridge):
    value = read_json(Path(bridge.root) / "file-transfer-settings.json", {})
    folder = value.get("receive_folder") if isinstance(value, dict) else None
    if not isinstance(folder, str) or not folder:
        return None
    path = Path(folder).resolve()
    if path == Path(path.anchor) or not path.is_dir():
        return None
    return path


def _rows(bridge, server):
    value = bridge._rpc("transfer-direct-list", {}, server=server)
    rows = value.get("transfers") if isinstance(value, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def incoming(bridge, server):
    """Offers addressed to this machine, or to this account before anyone claimed one."""
    account = bridge.identity["account_id"]
    node = bridge.profile["node"]
    result = []
    for row in _rows(bridge, server):
        if row.get("from_node") == node:
            continue
        addressed = row.get("to_node") == node or (row.get("to_node") is None and row.get("recipient_id") == account)
        if not addressed or row.get("state") not in INCOMING_STATES:
            continue
        result.append(row)
    return result


def outgoing(bridge, server):
    """What this machine is sending, so the page can show its state."""
    node = bridge.profile["node"]
    return [
        row for row in _rows(bridge, server) if row.get("from_node") == node and row.get("state") in INCOMING_STATES
    ]


def respond(bridge, server, transfer_id, accept):
    keys = _keypair(bridge.root)
    return bridge._rpc(
        "transfer-direct-respond",
        {
            "id": transfer_id,
            "accept": accept,
            "node": bridge.profile["node"],
            "claim_key": keys["public_key"],
        },
        server=server,
    )


def accept(bridge, server, transfer_id):
    """Claim an offer for this machine, remember the sender, and start pulling."""
    row = next((item for item in incoming(bridge, server) if item.get("id") == transfer_id), None)
    if row is None:
        raise ValueError("这条传输已经处理或不属于本机")
    if receive_folder(bridge) is None:
        raise ValueError("请先设置本机默认接收目录")
    result = respond(bridge, server, transfer_id, True)
    owner = row.get("owner_id")
    if isinstance(owner, str) and owner:
        transfer_trust.remember(bridge.root, owner)
    row = dict(row, state="accepted", to_node=bridge.profile["node"])
    _start(bridge, server, row)
    return {"accepted": True, "id": transfer_id, "state": result.get("state") if isinstance(result, dict) else None}


def reject(bridge, server, transfer_id):
    respond(bridge, server, transfer_id, False)
    return {"accepted": False, "id": transfer_id}


def consume(bridge, server):
    """Run on every dashboard poll: claim what may be claimed, pull what is claimed."""
    try:
        publish(bridge, server)
        rows = incoming(bridge, server)
    except (OSError, RuntimeError, ValueError, PermissionError):
        return
    folder = receive_folder(bridge)
    node = bridge.profile["node"]
    for row in rows:
        if row.get("state") == "queued":
            if not (folder and transfer_trust.is_trusted(bridge.root, row.get("owner_id"))):
                continue
            try:
                respond(bridge, server, row["id"], True)
            except (OSError, RuntimeError, ValueError, PermissionError):
                continue
            row = dict(row, state="accepted", to_node=node)
        if row.get("state") == "accepted" and row.get("to_node") == node:
            _start(bridge, server, row)


def _start(bridge, server, row):
    folder = receive_folder(bridge)
    if folder is None:
        return
    transfer_id = row.get("id")
    with _LOCK:
        if transfer_id in _INFLIGHT:
            return
        _INFLIGHT.add(transfer_id)
    thread = threading.Thread(
        target=_pull,
        args=(bridge, server, dict(row), folder),
        daemon=True,
        name="direct-transfer-receive",
    )
    thread.start()


def _report(bridge, server, transfer_id, transferred, state, error=None):
    payload = {"id": transfer_id, "transferred": int(transferred or 0), "state": state}
    if error:
        payload["error"] = str(error)[:500]
    try:
        bridge._rpc("transfer-direct-progress", payload, server=server)
    except Exception:  # progress is advisory; a failed report must not kill a transfer
        pass


def _throttled(bridge, server, transfer_id, transferred, size):
    now = time.time()
    with _LOCK:
        last = _PROGRESS_AT.get(transfer_id, 0.0)
        if now - last < 1.5:
            return
        _PROGRESS_AT[transfer_id] = now
    _report(bridge, server, transfer_id, transferred, "running")


def _pull(bridge, server, row, folder):
    transfer_id = row.get("id")
    size = row.get("size") or 0
    destination = folder / safe_name(row.get("name") or "file")
    url = "http://" + str(row.get("address")) + "/transfer/" + str(transfer_id)
    keys = _keypair(bridge.root)
    try:
        _report(bridge, server, transfer_id, 0, "running")
        for _attempt in range(12):
            try:
                meta = direct_transfer.probe(url, keys["private_key"], keys["public_key"], transfer_id, timeout=10)
                direct_transfer.fetch(
                    url,
                    keys["private_key"],
                    keys["public_key"],
                    transfer_id,
                    destination,
                    expect_size=meta.get("size") or size,
                    expect_digest=meta.get("sha256"),
                    expect_mtime=meta.get("mtime"),
                    progress=lambda done: _throttled(bridge, server, transfer_id, done, size),
                )
                break
            except direct_transfer.TransferPending:
                time.sleep(1.0)
        else:
            raise direct_transfer.TransferError("发送方仍未确认接收设备")
        _report(bridge, server, transfer_id, size, "done")
    except Exception as exc:  # one failed transfer must not break the poll loop
        _report(bridge, server, transfer_id, 0, "failed", exc)
    finally:
        with _LOCK:
            _INFLIGHT.discard(transfer_id)
            _PROGRESS_AT.pop(transfer_id, None)


def consume_all(bridge):
    _watch(bridge)
    for server in bridge._services():
        consume(bridge, server)


def start(bridge):
    """Begin receiving for the lifetime of the session; safe to call on every refresh."""
    _watch(bridge)


def _watch(bridge):
    """Keep receiving while the app is open, not only while the page is rendered."""
    with _LOCK:
        thread = _WATCH["thread"]
        if thread is not None and thread.is_alive():
            return

        def run():
            while True:
                try:
                    if not bridge.identity:
                        return
                except Exception:
                    return
                try:
                    for server in bridge._services():
                        consume(bridge, server)
                except Exception:  # a hub hiccup must not stop receiving
                    pass
                time.sleep(WATCH_SECONDS)

        thread = threading.Thread(target=run, daemon=True, name="direct-transfer-receive-watch")
        _WATCH["thread"] = thread
        thread.start()
