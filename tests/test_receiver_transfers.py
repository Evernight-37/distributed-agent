"""The receiver consumes offers: auto-accept only for known senders with a folder."""

import tempfile
import unittest
from pathlib import Path

from agent_harness import transfer_trust
from agent_harness.common import read_json, write_json
from agent_harness.pull_worker import Connector


class FakeConnector(Connector):
    """No network: every hub call is recorded, transfers fail at the socket."""

    def __init__(self, root, node="node-a", folder=None):
        self.root = Path(root)
        self.config_path = self.root / "pull-config.json"
        self.config = {"node": node, "server": "http://127.0.0.1:1", "device_token": "x" * 40}
        write_json(self.config_path, self.config)
        self.calls = []
        if folder is not None:
            write_json(self.root / "file-transfer-settings.json", {"version": 1, "receive_folder": str(folder)})

    def transport(self, config, operation, payload=None):
        self.calls.append((operation, payload))
        return {}

    def transfer_signing_key(self):
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

        key = Ed25519PrivateKey.generate()
        return {
            "private_key": base64.b64encode(
                key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            ).decode(),
            "public_key": base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(),
        }


class ReceiverTransferTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()

    def offer(self, **overrides):
        row = {
            "id": "transfer-1",
            "to_node": "node-a",
            "from_node": "node-b",
            "owner_id": "account-sender",
            "name": "dump.bin",
            "size": 4,
            "address": "127.0.0.1:9",
            "state": "queued",
        }
        row.update(overrides)
        return row

    def test_an_unknown_sender_waits_for_the_owner(self):
        connector = FakeConnector(self.root, folder=self.inbox)
        connector._consume_transfers([self.offer()])
        pending = read_json(self.root / "pending-transfers.json")
        self.assertEqual([item["id"] for item in pending["transfers"]], ["transfer-1"])
        self.assertEqual(connector.calls, [])

    def test_a_known_sender_with_a_folder_is_accepted_and_attempted(self):
        transfer_trust.remember(self.root, "account-sender")
        connector = FakeConnector(self.root, folder=self.inbox)
        connector._consume_transfers([self.offer()])
        operations = [call[0] for call in connector.calls]
        self.assertEqual(operations[0], "transfer-respond")
        self.assertIn("transfer-progress", operations)
        self.assertEqual(connector.calls[-1][1]["state"], "failed")

    def test_a_fresh_receiver_falls_back_to_its_local_default_folder(self):
        # Enrollment does not create a settings file; same-account delivery must
        # still work, so the receiver keeps using its own received-files folder.
        transfer_trust.remember(self.root, "account-sender")
        connector = FakeConnector(self.root, folder=None)
        connector._consume_transfers([self.offer()])
        self.assertEqual(connector.calls[0][0], "transfer-respond")
        self.assertEqual(connector.calls[-1][1]["state"], "failed")  # address is a dead port

    def test_an_untrusted_sender_is_not_auto_accepted_even_with_the_default(self):
        connector = FakeConnector(self.root, folder=None)
        connector._consume_transfers([self.offer(owner_id="account-stranger")])
        self.assertEqual(connector.calls, [])
        self.assertTrue((self.root / "pending-transfers.json").exists())

    def test_offers_for_another_node_are_ignored(self):
        connector = FakeConnector(self.root, folder=self.inbox)
        connector._consume_transfers([self.offer(to_node="node-z")])
        self.assertEqual(connector.calls, [])
        self.assertFalse((self.root / "pending-transfers.json").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
