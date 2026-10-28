import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.common import read_json, write_json
from agent_harness.headless_transfers import HeadlessTransferReceiver, _DeviceBridge


class FakeConnector:
    def __init__(self, root):
        self.root = Path(root)
        self.config = {
            "server": "https://worker.example.test",
            "node": "linux-node",
            "broker_proxy": "http://127.0.0.1:7890",
        }
        self.calls = []
        self.transfers = []

    def transport(self, config, operation, payload):
        self.asserted_config = config
        self.calls.append((operation, payload))
        if operation == "transfers":
            return {"transfers": list(self.transfers), "node": "linux-node"}
        return {"operation": operation, **payload}


class HeadlessTransferTests(unittest.TestCase):
    def test_device_bridge_uses_receiver_credentials_and_operation_allowlist(self):
        with tempfile.TemporaryDirectory() as temporary:
            connector = FakeConnector(temporary)
            bridge = _DeviceBridge(connector)
            self.assertEqual(bridge.broker_proxy, "http://127.0.0.1:7890")
            result = bridge._rpc("transfer-download-ticket", {"id": "transfer-one"})
            self.assertEqual(result["operation"], "transfer-download-ticket")
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                bridge._rpc("transfer-cancel", {"id": "transfer-one"})

    def test_ready_transfer_downloads_in_background_to_receiver_local_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            connector = FakeConnector(temporary)
            connector.transfers = [
                {
                    "id": "transfer-" + "a" * 32,
                    "name": "report.txt",
                    "size": 7,
                    "state": "ready",
                }
            ]
            manager = HeadlessTransferReceiver(connector)

            def fake_download(_bridge, _server, _transfer_id, destination):
                destination.write_bytes(b"content")
                return {"saved": True, "name": destination.name, "size": 7}

            with patch("agent_harness.headless_transfers.download", side_effect=fake_download):
                result = manager.reconcile()
                self.assertEqual(result["assigned"], 1)
                deadline = time.monotonic() + 5
                while manager.running and time.monotonic() < deadline:
                    time.sleep(0.01)

            self.assertFalse(manager.running)
            jobs = read_json(manager.path)["jobs"]
            job = jobs[connector.transfers[0]["id"]]
            destination = Path(job["destination"])
            self.assertEqual(job["state"], "completed")
            self.assertEqual(destination.parent, (Path(temporary) / "received-files").resolve())
            self.assertEqual(destination.read_bytes(), b"content")

    def test_completed_transfer_is_not_started_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            connector = FakeConnector(temporary)
            connector.transfers = [
                {
                    "id": "transfer-" + "b" * 32,
                    "name": "once.bin",
                    "size": 1,
                    "state": "ready",
                }
            ]
            manager = HeadlessTransferReceiver(connector)
            calls = []

            def fake_download(_bridge, _server, _transfer_id, destination):
                calls.append(destination)
                destination.write_bytes(b"x")
                return {"saved": True, "name": destination.name, "size": 1}

            with patch("agent_harness.headless_transfers.download", side_effect=fake_download):
                manager.reconcile()
                deadline = time.monotonic() + 5
                while manager.running and time.monotonic() < deadline:
                    time.sleep(0.01)
                manager.reconcile()
            self.assertEqual(len(calls), 1)

    def test_configured_receive_folder_is_used_for_new_transfer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "selected"
            selected.mkdir()
            write_json(
                root / "file-transfer-settings.json",
                {"version": 1, "receive_folder": str(selected)},
            )
            connector = FakeConnector(root)
            transfer = {
                "id": "transfer-" + "c" * 32,
                "name": "chosen.txt",
                "size": 1,
                "state": "ready",
            }
            manager = HeadlessTransferReceiver(connector)
            destination = manager._destination(transfer)
            self.assertEqual(destination.parent, selected.resolve())


if __name__ == "__main__":
    unittest.main()
