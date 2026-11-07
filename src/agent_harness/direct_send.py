"""Starting and stopping a direct send from the desktop."""

import secrets
import threading
import time
from pathlib import Path

from agent_harness import direct_transfer

SENDERS = {}
WATCH_SECONDS = 1.0
_WATCHER = {"thread": None}
_WATCH_LOCK = threading.RLock()
_SERVERS = {}


def _device(bridge, node, server):
    for device in bridge._dashboard().get("devices", []):
        if device.get("node") == node and device.get("server") == server:
            return device
    return None


def send(bridge, source, to_node=None, recipient=None, server=None):
    source = Path(source)
    if not source.is_file() or source.is_symlink():
        raise ValueError("仅支持普通文件")
    if to_node:
        device = _device(bridge, to_node, server)
        if not device or not device.get("transfer_public_key"):
            raise ValueError("目标设备还不支持直连速传；请先把它更新到当前版本并保持在线")
        public_key = device["transfer_public_key"]
    elif recipient:
        # The teammate's device is unknown until one of them claims the offer;
        # the watcher installs its key as soon as that happens.
        public_key = ""
    else:
        raise ValueError("请选择接收设备或接收账户")
    address = direct_transfer.local_tailnet_address()
    if not address:
        raise ValueError("本机没有可用的 Tailscale 地址，无法建立直连")
    # The id is chosen here and given to the service, so the id this sender
    # serves and the id the receiver is told can never drift apart.
    transfer_id = "transfer-" + secrets.token_hex(16)
    sender = direct_transfer.Sender(source, transfer_id, public_key, host=address)
    port = sender.start()
    try:
        offer = bridge._rpc(
            "transfer-direct-offer",
            {
                "id": transfer_id,
                "to_node": to_node,
                "recipient": recipient,
                "name": source.name,
                "size": sender.size,
                "address": "%s:%d" % (address, port),
            },
            server=server,
        )
    except Exception:
        sender.stop()
        raise
    SENDERS[transfer_id] = sender
    _SERVERS[transfer_id] = server
    _watch(bridge)
    return {
        "selected": True,
        "id": transfer_id,
        "size": sender.size,
        "address": "%s:%d" % (address, port),
        "state": (offer or {}).get("state"),
    }


def observe(bridge, server):
    """Learn who claimed an outgoing transfer, and retire finished senders."""
    try:
        listed = bridge._rpc("transfer-direct-list", {}, server=server)
    except Exception:
        return {"observed": False}
    rows = (listed or {}).get("transfers") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sender = SENDERS.get(row.get("id"))
        if sender is None:
            continue
        claim = row.get("claim_key")
        if isinstance(claim, str) and claim and claim != sender.recipient_public_key:
            sender.set_recipient(claim)
        if row.get("state") in {"rejected", "done", "failed"}:
            cancel(row["id"])
    return {"observed": True}


def _watch(bridge):
    """Keep learning claims while anything is being sent.

    Reading the claim only when the transfers page renders made a cross-account
    transfer depend on the sender staring at that page, so this runs on its own
    tick and stops when nothing is left to send. The bridge is looked up on every
    tick rather than captured: a watcher that outlives one desktop session must
    not keep asking the old one.
    """
    with _WATCH_LOCK:
        _WATCHER["bridge"] = bridge
        current = _WATCHER["thread"]
        if current is not None and current.is_alive():
            return
        thread = threading.Thread(target=_watch_loop, daemon=True, name="direct-transfer-watch")
        _WATCHER["thread"] = thread
        thread.start()


def _watch_loop():
    while True:
        with _WATCH_LOCK:
            if not SENDERS:
                if _WATCHER["thread"] is threading.current_thread():
                    _WATCHER["thread"] = None
                return
            bridge = _WATCHER["bridge"]
            servers = {value for value in _SERVERS.values() if value}
        for server in servers:
            try:
                observe(bridge, server)
            except Exception:  # the service may be down; the sender keeps serving
                pass
        time.sleep(WATCH_SECONDS)


def cancel(transfer_id):
    sender = SENDERS.pop(transfer_id, None)
    _SERVERS.pop(transfer_id, None)
    if sender is None:
        return {"cancelled": False}
    sender.stop()
    return {"cancelled": True}
