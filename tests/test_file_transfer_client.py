import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.desktop_actions import DesktopTransferActions
from agent_harness.file_transfer_client import FileTransferManager


class Bridge:
    def __init__(self, root):
        self.root = Path(root)
        self.identity = {"account_id": "account-a"}
        self.profile = {"node": "device-a"}
        self.broker_proxy = None
        self.calls = []

    def _rpc(self, operation, payload=None, server=None, timeout=None):
        self.calls.append((operation, payload, server))
        if operation == "transfer-request":
            return {"id": "transfer-" + "1" * 32, "state": "requested"}
        if operation == "transfer-decide":
            return {"id": payload["id"], "state": "accepted"}
        return {"id": (payload or {}).get("id"), "state": "ready"}


def await_job(manager, job_id, states):
    for _ in range(100):
        job = manager.status(job_id)
        if job["state"] in states and job_id not in manager.running:
            return job
        time.sleep(0.02)
    raise AssertionError(manager.status(job_id))


class FileTransferClientTests(unittest.TestCase):
    def test_remote_receive_folder_is_sent_as_owner_command(self):
        class RemoteBridge:
            identity = {"account_id": "account-a"}
            profile = {"node": "desktop-a", "server": "https://relay.example"}

            def __init__(self):
                self.calls = []

            def _owner(self, action, node, data, server=None):
                self.calls.append((action, node, data, server))
                return {"id": "command-one", "state": "queued"}

        bridge = RemoteBridge()
        result = DesktopTransferActions(bridge).invoke(
            "file-transfer-receive-folder",
            {
                "server": "https://relay.example",
                "node": "linux-one",
                "path": "/srv/agent-harness/inbox",
            },
        )
        self.assertTrue(result["pending"])
        self.assertEqual(
            bridge.calls,
            [
                (
                    "transfer-receive-directory",
                    "linux-one",
                    {"path": "/srv/agent-harness/inbox"},
                    "https://relay.example",
                )
            ],
        )

    def test_request_is_persisted_without_exposing_source_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "large.bin"
            source.write_bytes(b"hello")
            bridge = Bridge(root)
            manager = FileTransferManager(bridge)
            job = manager.send("https://relay.example", "account-b", None, source)
            job = await_job(manager, job["id"], {"waiting", "paused"})
            self.assertEqual(job["phase"], "waiting_recipient")
            self.assertNotIn("source", job)
            self.assertTrue((root / "file-transfer-jobs.json").is_file())

    def test_reconcile_resumes_accepted_outgoing_transfer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "large.bin"
            source.write_bytes(b"hello")
            bridge = Bridge(root)
            manager = FileTransferManager(bridge)
            job = manager.send("https://relay.example", "account-b", None, source)
            job = await_job(manager, job["id"], {"waiting"})
            remote = {
                "id": job["transfer_id"],
                "server": "https://relay.example",
                "state": "accepted",
                "sender_id": "account-a",
                "recipient_id": "account-b",
                "recipient_node": None,
                "name": source.name,
                "size": source.stat().st_size,
            }
            with patch("agent_harness.file_transfer_client.upload", return_value={"state": "ready"}):
                manager.reconcile([remote])
                finished = await_job(manager, job["id"], {"completed"})
            self.assertEqual(finished["phase"], "completed")

    def test_same_account_target_uses_local_default_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            destination = root / "received"
            destination.mkdir()
            bridge = Bridge(root)
            manager = FileTransferManager(bridge)
            manager.set_receive_folder(destination)
            remote = {
                "id": "transfer-" + "2" * 32,
                "server": "https://relay.example",
                "state": "ready",
                "sender_id": "account-a",
                "recipient_id": "account-a",
                "recipient_node": "device-a",
                "name": "result.bin",
                "size": 12,
            }
            with patch("agent_harness.file_transfer_client.download", return_value={"saved": True}):
                manager.reconcile([remote])
                job = next(item for item in manager.jobs() if item.get("transfer_id") == remote["id"])
                await_job(manager, job["id"], {"completed"})
            self.assertFalse(any(call[0] == "transfer-decide" for call in bridge.calls))

    def test_same_account_manual_destination_skips_decision_rpc(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bridge = Bridge(root)
            manager = FileTransferManager(bridge)
            transfer = {
                "id": "transfer-" + "3" * 32,
                "state": "accepted",
                "sender_id": "account-a",
                "recipient_id": "account-a",
                "name": "same.bin",
                "size": 4,
            }
            manager.accept("https://relay.example", transfer, root / "same.bin")
            self.assertFalse(any(call[0] == "transfer-decide" for call in bridge.calls))


if __name__ == "__main__":
    unittest.main()
