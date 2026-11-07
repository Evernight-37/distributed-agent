"""Receiving a direct transfer in the desktop app, and the key a sender needs."""

import base64
import tempfile
import time
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from agent_harness import direct_receive, direct_transfer, transfer_trust
from agent_harness.account_backend import AccountBackend, device_node
from agent_harness.account_identity import generate_identity, sign_envelope
from agent_harness.broker import PullBackend
from agent_harness.collaboration_store import Store


def keypair():
    key = Ed25519PrivateKey.generate()
    return {
        "private_key": base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode(),
        "public_key": base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(),
    }


class TransferKeyTests(unittest.TestCase):
    """The service publishes each device's key; a sender cannot serve without it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.api = AccountBackend(self.store, PullBackend(self.store))
        self.identity = generate_identity()
        self.store.claim_local_host(self.identity["account_id"])
        self.call("register", {"name": "Test", "device_name": "Desk"}, "register")

    def call(self, operation, payload, action="rpc"):
        nonce = self.api.api({"action": "challenge", "public_key": self.identity["public_key"]})["nonce"]
        envelope = sign_envelope(
            self.identity,
            {
                "domain": "harness-account-request-v1",
                "nonce": nonce,
                "operation": operation,
                "payload": payload,
            },
        )
        return self.api.api({"action": action, "envelope": envelope})

    def test_a_published_key_reaches_the_peers_list(self):
        key = base64.b64encode(b"k" * 32).decode()
        published = self.call("transfer-key-publish", {"transfer_public_key": key})
        self.assertEqual(published["node"], device_node(self.identity["public_key"]))
        peers = self.call("transfer-peers", {})
        device = next(item for item in peers["devices"] if item["node"] == published["node"])
        self.assertEqual(device["transfer_public_key"], key)

    def test_an_invalid_key_is_refused(self):
        for value in ("", "not base64!", base64.b64encode(b"short").decode()):
            with self.assertRaises(ValueError):
                self.call("transfer-key-publish", {"transfer_public_key": value})

    def test_a_device_that_never_published_has_no_key(self):
        peers = self.call("transfer-peers", {})
        for device in peers["devices"]:
            self.assertNotIn("transfer_public_key", device)


class FakeBridge:
    """The desktop's view of the service, with a real sender behind the address."""

    def __init__(self, root, sender):
        self.root = Path(root)
        self.profile = {"node": "device-receiver", "server": "http://hub"}
        self.identity = {"account_id": "account-me", "public_key": "p" * 44}
        self.sender = sender
        self.rows = {}
        self.progress = []
        self.keys = []

    def _services(self):
        return ["http://hub"]

    def _rpc(self, operation, payload=None, server=None):
        payload = payload or {}
        if operation == "transfer-key-publish":
            self.keys.append(payload["transfer_public_key"])
            return {"published": True}
        if operation == "transfer-direct-list":
            return {"transfers": list(self.rows.values())}
        if operation == "transfer-direct-respond":
            row = self.rows[payload["id"]]
            row["state"] = "accepted" if payload["accept"] else "rejected"
            row["to_node"] = payload.get("node") or row.get("to_node")
            row["claim_key"] = payload.get("claim_key") or row.get("claim_key")
            # The sender's own watcher learns the claim a moment later.
            if row["claim_key"] and self.sender is not None:
                self.sender.set_recipient(row["claim_key"])
            return dict(row)
        if operation == "transfer-direct-progress":
            self.progress.append(dict(payload))
            row = self.rows.get(payload["id"])
            if row is not None and payload.get("state"):
                row["state"] = payload["state"]
                row["transferred"] = payload.get("transferred") or 0
            return {"id": payload["id"]}
        raise AssertionError("unexpected operation %s" % operation)


class DesktopReceiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        (self.root / "file-transfer-settings.json").write_text(
            '{"receive_folder": "%s"}' % str(self.inbox).replace("\\", "/"), encoding="utf-8"
        )
        self.payload = b"direct-to-desktop" * 4096
        self.source = self.root / "dump.bin"
        self.source.write_bytes(self.payload)
        self.transfer_id = "transfer-" + "c" * 32
        self.sender = direct_transfer.Sender(self.source, self.transfer_id, "", host="127.0.0.1")
        port = self.sender.start()
        self.addCleanup(self.sender.stop)
        self.bridge = FakeBridge(self.root, self.sender)
        self.bridge.rows[self.transfer_id] = {
            "id": self.transfer_id,
            "from_node": "device-sender",
            "to_node": None,
            "owner_id": "account-other",
            "recipient_id": "account-me",
            "name": "dump.bin",
            "size": len(self.payload),
            "address": "127.0.0.1:%d" % port,
            "state": "queued",
            "transferred": 0,
        }

    def wait_for(self, predicate, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def test_an_accepted_offer_is_pulled_straight_to_the_folder(self):
        result = direct_receive.accept(self.bridge, "http://hub", self.transfer_id)
        self.assertTrue(result["accepted"])
        arrived = self.inbox / "dump.bin"
        self.assertTrue(self.wait_for(arrived.exists), self.bridge.progress)
        self.assertEqual(arrived.read_bytes(), self.payload)
        self.assertTrue(self.wait_for(lambda: self.bridge.rows[self.transfer_id]["state"] == "done"))
        self.assertIn("done", [item.get("state") for item in self.bridge.progress])

    def test_a_trusted_sender_is_received_without_asking(self):
        transfer_trust.remember(self.root, "account-other")
        direct_receive.consume(self.bridge, "http://hub")
        arrived = self.inbox / "dump.bin"
        self.assertTrue(self.wait_for(arrived.exists), self.bridge.progress)
        self.assertEqual(arrived.read_bytes(), self.payload)

    def test_an_unknown_sender_waiting_for_confirmation_is_not_pulled(self):
        direct_receive.consume(self.bridge, "http://hub")
        time.sleep(0.5)
        self.assertEqual(self.bridge.rows[self.transfer_id]["state"], "queued")
        self.assertFalse((self.inbox / "dump.bin").exists())

    def test_without_a_receive_folder_nothing_is_accepted(self):
        transfer_trust.remember(self.root, "account-other")
        (self.root / "file-transfer-settings.json").write_text("{}", encoding="utf-8")
        direct_receive.consume(self.bridge, "http://hub")
        time.sleep(0.3)
        self.assertEqual(self.bridge.rows[self.transfer_id]["state"], "queued")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
