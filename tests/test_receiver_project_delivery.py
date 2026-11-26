import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_harness.common import read_json, write_json
from agent_harness.pull_worker import Connector


class ReceiverProjectDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "receiver.json"
        write_json(self.config, {"server": "http://127.0.0.1:9"})
        self.action = {"kind": "deliver", "summary": " Result ", "links": [], "attachments": ["result.txt"]}
        self.delivery = {"summary": "Result", "links": [], "attachments": ["result.txt"]}
        self.pending = {"ok": True, "status": "pending_attachment_delivery", "delivery": self.delivery}
        self.transport = Mock(return_value=self.pending)
        self.executor = Mock(return_value={"status": "needs_review", "report": '{"reply":"worker prose"}'})
        self.connector = Connector(self.config, self.transport, self.executor)
        self.job = {"id": "delivery", "lease_id": "lease", "kind": "task", "request": {}}
        self.receipt = self.connector.accept(self.job)
        self.receipt.update(phase="running", project_tools=True, project_file_delivery=True)
        self.path = self.connector.receipt_path("delivery")
        write_json(self.path, self.receipt)
        self.mailbox = self.root / "jobs" / self.receipt["local_id"]
        self.mailbox.mkdir(parents=True)
        self.request = self.mailbox / ("pt-request-" + "a" * 32 + ".json")
        self.result = self.mailbox / self.request.name.replace("request", "result")
        write_json(self.request, {"action": self.action})

    def test_file_tool_persists_the_hub_reservation_before_returning_pending(self):
        self.connector._service_project_tools(self.receipt, False)
        self.transport.assert_called_once_with(
            self.connector.config, "project-tool", {"job_id": "delivery", "lease_id": "lease", "action": self.action}
        )
        self.assertEqual(read_json(self.result), {"ok": True, "status": "pending_attachment_delivery"})
        self.assertEqual(read_json(self.path)["staged_file_delivery"], self.delivery)

        # A receiver restart keeps the accepted intent. The existing uploader
        # will process this report using its normal approval and lease flow.
        restarted = Connector(self.config, self.transport, self.executor)
        result = restarted.advance(read_json(self.path))
        self.assertEqual(result["phase"], "terminal")
        self.assertEqual(
            json.loads(result["result"]["report"]),
            {
                "reply": "",
                "project_action": {"kind": "deliver", "summary": "Result", "links": []},
                "attachments": [{"path": "result.txt"}],
                "project_file_delivery": True,
            },
        )
        self.transport.assert_called_once()

    def test_missing_hub_response_leaves_the_same_request_unacknowledged(self):
        self.transport.side_effect = OSError("connection broken")
        self.connector._service_project_tools(self.receipt, False)
        self.assertFalse(self.result.exists())
        self.assertNotIn("staged_file_delivery", read_json(self.path))
        self.transport.side_effect = None
        self.connector._service_project_tools(self.receipt, False)
        self.assertEqual(self.transport.call_args_list[0], self.transport.call_args_list[1])
        self.assertEqual(read_json(self.result)["status"], "pending_attachment_delivery")

    def test_mismatched_or_unsupported_reservation_cannot_stage_a_file(self):
        self.receipt["project_file_delivery"] = False
        self.connector._service_project_tools(self.receipt, False)
        self.transport.assert_not_called()
        self.assertEqual(read_json(self.result)["error_code"], "action_not_allowed")
        self.result.unlink()
        self.receipt["project_file_delivery"] = True
        self.transport.return_value = {
            **self.pending,
            "delivery": {**self.delivery, "attachments": ["different.txt"]},
        }
        self.connector._service_project_tools(self.receipt, False)
        self.assertEqual(read_json(self.result)["error_code"], "invalid_response")
        self.assertNotIn("staged_file_delivery", read_json(self.path))

    def test_cancelled_reservation_and_executor_supplied_metadata_cannot_upload(self):
        self.connector._service_project_tools(self.receipt, False)
        cancelled = self.connector.advance(read_json(self.path), cancelled=True)
        self.assertEqual(cancelled["result"]["status"], "cancelled")
        self.assertNotIn("report", cancelled["result"])

        bogus = {
            "reply": "example",
            "project_file_delivery": True,
            "delivery_attachment_count": 1,
            "delivery_attachment_error": "spoofed",
            "attachment_ids": ["nonempty"],
        }
        untrusted = {"result": {"status": "needs_review", "report": json.dumps(bogus)}}
        self.connector._finalize_project_delivery(untrusted, False)
        self.assertEqual(json.loads(untrusted["result"]["report"]), {"reply": "example"})

    def test_cancellation_after_restart_prevents_a_terminal_file_upload(self):
        self.connector._service_project_tools(self.receipt, False)
        terminal = self.connector.advance(read_json(self.path))
        self.assertEqual(terminal["result"]["status"], "needs_review")
        self.transport.reset_mock()

        def transport(config, operation, payload):
            if operation == "poll":
                return {"features": [], "cancellations": ["delivery"]}
            if operation == "report":
                self.assertEqual(payload["state"], "cancelled")
                self.assertNotIn("report", payload["result"])
                return {}
            self.fail(f"Cancelled receiver unexpectedly called {operation}")

        self.transport.side_effect = transport
        restarted = Connector(self.config, self.transport, self.executor)
        with patch.object(restarted, "_health", return_value={"status": "healthy"}):
            restarted.cycle(wait_seconds=0)
        self.assertTrue(read_json(self.path)["reported_terminal"])
