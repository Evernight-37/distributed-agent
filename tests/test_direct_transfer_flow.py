"""End to end: a desktop offers, a receiver pulls, over one real local socket.

The bug this covers: the sender used to serve its own random id while the service
minted a different one, so every signed request failed with 403 and neither side
could tell why. The fake service below mints an id only when the caller does not
supply one, exactly like the real one.
"""

import base64
import hashlib
import os
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from agent_harness import direct_send, transfer_trust
from agent_harness.pull_worker import Connector


def keypair():
    key = Ed25519PrivateKey.generate()
    return {
        "private_key": base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode(),
        "public_key": base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(),
    }


class FakeHub:
    """Just enough of the collaboration service: offers, claims, progress."""

    def __init__(self):
        self.rows = {}
        self.minted = 0

    def rpc(self, operation, payload=None, server=None):
        payload = payload or {}
        if operation == "transfer-direct-offer":
            self.minted += 1
            transfer_id = payload.get("id") or ("transfer-%032x" % self.minted)
            self.rows[transfer_id] = {
                "id": transfer_id,
                "to_node": payload.get("to_node"),
                "from_node": "desk",
                "owner_id": "account-sender",
                "recipient_id": payload.get("recipient") or "account-sender",
                "name": payload["name"],
                "size": payload["size"],
                "address": payload["address"],
                "state": "queued" if payload.get("recipient") else "accepted",
                "transferred": 0,
                "claim_key": None,
            }
            return dict(self.rows[transfer_id])
        if operation == "transfer-direct-list":
            return {"transfers": list(self.rows.values())}
        raise AssertionError("unexpected hub operation %s" % operation)

    def respond(self, transfer_id, accept, node=None, claim_key=None):
        row = self.rows[transfer_id]
        row["state"] = "accepted" if accept else "rejected"
        row["to_node"] = node or row["to_node"]
        row["claim_key"] = claim_key or row["claim_key"]
        return dict(row)

    def progress(self, transfer_id, transferred, state=None):
        row = self.rows[transfer_id]
        row["transferred"] = transferred
        if state:
            row["state"] = state
        return dict(row)


class Desktop:
    def __init__(self, hub, device_key=None):
        self.hub = hub
        self.device_key = device_key
        self.profile = {"server": "http://hub", "node": "desk"}

    def _dashboard(self):
        devices = []
        if self.device_key:
            devices.append({"node": "peer", "server": "http://hub", "transfer_public_key": self.device_key})
        return {"devices": devices}

    def _rpc(self, operation, payload=None, server=None):
        return self.hub.rpc(operation, payload, server)


class Receiver(Connector):
    def __init__(self, root, hub, key=None):
        self.root = Path(root)
        self.config_path = self.root / "pull-config.json"
        self.config = {"node": "peer", "server": "http://hub"}
        self.hub = hub
        self.calls = []
        self.key = key or keypair()

    def transfer_signing_key(self):
        return self.key

    def transport(self, config, operation, payload=None):
        self.calls.append((operation, payload))
        payload = payload or {}
        if operation == "transfer-respond":
            self.hub.respond(payload["transfer_id"], payload["accept"], "peer", payload.get("transfer_public_key"))
        elif operation == "transfer-progress":
            self.hub.progress(payload["transfer_id"], payload["transferred"], payload.get("state"))
        return {}


class DirectTransferFlowTests(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile

        self.folder = tempfile.mkdtemp(prefix="flow-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.root = Path(self.folder)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        (self.root / "file-transfer-settings.json").write_text(
            '{"version": 1, "receive_folder": "%s"}' % str(self.inbox).replace("\\", "/"), encoding="utf-8"
        )
        transfer_trust.remember(self.root, "account-sender")
        self.hub = FakeHub()
        self.payload = os.urandom(200_000)
        self.source = self.root / "dump.bin"
        self.source.write_bytes(self.payload)
        patcher = mock.patch.dict(os.environ, {"TAILSCALE_IP": "127.0.0.1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for transfer_id in list(direct_send.SENDERS):
            direct_send.cancel(transfer_id)

    def test_a_device_of_my_own_account_receives_the_file(self):
        receiver = Receiver(self.root, self.hub)
        desktop = Desktop(self.hub, receiver.key["public_key"])
        sent = direct_send.send(desktop, self.source, to_node="peer", server="http://hub")
        # The service must adopt the sender's id, not mint its own.
        self.assertIn(sent["id"], self.hub.rows)

        receiver._consume_transfers([self.hub.rows[sent["id"]]])
        arrived = self.inbox / "dump.bin"
        self.assertTrue(arrived.exists(), receiver.calls)
        self.assertEqual(arrived.read_bytes(), self.payload)

    def test_a_teammate_claims_and_the_bytes_still_arrive(self):
        desktop = Desktop(self.hub)
        sent = direct_send.send(desktop, self.source, recipient="account-peer", server="http://hub")
        receiver = Receiver(self.root, self.hub)
        # respond -> 409 until the sender learns the claim -> retry -> success
        receiver._consume_transfers([self.hub.rows[sent["id"]]])
        arrived = self.inbox / "dump.bin"
        self.assertTrue(arrived.exists(), receiver.calls)
        self.assertEqual(hashlib.sha256(arrived.read_bytes()).hexdigest(), hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(self.hub.rows[sent["id"]]["state"], "done")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
