"""Starting and stopping a direct send from the desktop."""

import secrets
from pathlib import Path

from agent_harness import direct_transfer

SENDERS = {}


def _device(bridge, node, server):
    for device in bridge._dashboard().get("devices", []):
        if device.get("node") == node and device.get("server") == server:
            return device
    return None


def send(bridge, source, to_node, server):
    source = Path(source)
    if not source.is_file() or source.is_symlink():
        raise ValueError("仅支持普通文件")
    device = _device(bridge, to_node, server)
    if not device or not device.get("transfer_public_key"):
        raise ValueError("目标设备还不支持直连速传；请先把它更新到当前版本并保持在线")
    address = direct_transfer.local_tailnet_address()
    if not address:
        raise ValueError("本机没有可用的 Tailscale 地址，无法建立直连")
    transfer_id = "transfer-" + secrets.token_hex(16)
    sender = direct_transfer.Sender(source, transfer_id, device["transfer_public_key"], host=address)
    port = sender.start()
    try:
        offer = bridge._rpc(
            "transfer-direct-offer",
            {
                "to_node": to_node,
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


def cancel(transfer_id):
    sender = SENDERS.pop(transfer_id, None)
    if sender is None:
        return {"cancelled": False}
    sender.stop()
    return {"cancelled": True}
