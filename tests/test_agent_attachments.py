"""Agent files must stay scoped to the assigned workspace, lease and conversation."""

import base64
import json
import os
import secrets
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_harness.agent_attachments import APPROVAL_FEATURE, prepare_reply, project_files_supported, read_project_file
from agent_harness.account_backend import AccountBackend
from agent_harness.attachments import AGENT_ATTACHMENT_FEATURE, Attachments, MAX_FILE_BYTES
from agent_harness.broker import PullBackend
from agent_harness.collaboration_store import Store
from agent_harness.common import read_json, write_json
from agent_harness.coordinator import Coordinator
from agent_harness.panel import handler_for
from agent_harness.pull_worker import api
from tests.helpers import provision_account


class AgentAttachmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "hub")
        self.owner = provision_account(self.store, "owner", bootstrap=True)["id"]
        self.project = self.store.project_create(self.owner, "Agent files")["id"]
        self.viewer = provision_account(
            self.store, "viewer", invite=self.store.invite(self.owner, self.project)["invite"]
        )["id"]
        self.backend = PullBackend(self.store)
        pair = self.backend.create_pair(self.owner, "node")
        self.token = secrets.token_urlsafe(32)
        self.backend.device_api("/api/device/enroll", {"pair_code": pair["pair_code"], "device_token": self.token})
        self.backend.device_api(
            "/api/device/poll",
            {
                "bindings": [
                    {
                        "project_id": self.project,
                        "backend_project": "work",
                        "name": "Agent",
                        "model": "model",
                        "effort": "low",
                        "timeout": 120,
                        "enabled": True,
                        "stages": ["discuss"],
                    }
                ]
            },
            self.token,
        )
        with self.store.connect() as db:
            self.agent = dict(db.execute("SELECT * FROM agents WHERE node='node'").fetchone())
        self.store.set_admin(self.owner, self.project, self.agent["id"])
        self.coordinator = Coordinator(self.store, self.backend)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.content = b"%PDF-1.4\n" + b"report data\n" * 60000
        (self.workspace / "report.pdf").write_bytes(self.content)
        self.files = Attachments(self.store)

    def task(self, private=False):
        self.store.message(
            self.owner, self.project, "Send report.pdf as an attachment", self.agent["id"] if private else None
        )
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        return self.backend.device_api("/api/device/poll", {}, self.token)["jobs"][0]

    def upload(self, job, **extra):
        payload = {
            "job_id": job["id"],
            "lease_id": job["lease_id"],
            "slot": 0,
            "name": "report.pdf",
            "base64": base64.b64encode(self.content).decode(),
        }
        payload.update(extra)
        return self.backend.device_api("/api/device/task-attachment", payload, self.token)

    def complete(self, job, ids):
        self.backend.device_api(
            "/api/device/report",
            {
                "job_id": job["id"],
                "lease_id": job["lease_id"],
                "state": "needs_review",
                "result": {"report": json.dumps({"reply": "Requested report", "attachment_ids": ids})},
            },
            self.token,
        )
        self.coordinator.collect()

    def connector_receipt(self, job, transport=None, policy="full"):
        state = self.root / "receiver"
        state.mkdir(exist_ok=True)
        receipt_path = state / "receipt.json"
        connector = SimpleNamespace(
            root=state,
            config={},
            receipt_path=lambda _: receipt_path,
            binding_for=lambda _: {"attachment_policy": policy},
            transport=transport
            or (lambda _, op, payload: self.backend.device_api("/api/device/" + op, payload, self.token)),
        )
        write_json(state / "jobs/local/request.json", {"project": {"remote_path": str(self.workspace)}})
        receipt = {
            "job": job,
            "local_id": "local",
            "phase": "terminal",
            "result": {
                "status": "needs_review",
                "report": json.dumps({"reply": "Requested report", "attachments": [{"path": "report.pdf"}]}),
            },
        }
        return connector, receipt

    def test_public_attachment_is_downloadable_and_upload_retry_is_idempotent(self):
        job = self.task()
        file = self.upload(job)
        self.assertEqual(self.upload(job), file)
        with self.assertRaises(PermissionError):
            self.files.download(self.owner, self.project, file["id"])
        self.complete(job, [file["id"]])
        message = self.store.project_view(self.viewer, self.project)["messages"][-1]
        self.assertEqual(message["sender_id"], self.agent["id"])
        self.assertEqual(message["attachments"], [file])
        self.assertEqual(
            base64.b64decode(self.files.download(self.viewer, self.project, file["id"])["base64"]), self.content
        )
        with self.assertRaises(PermissionError):
            self.upload(job, slot=1)
        with self.assertRaises(ValueError):
            self.upload(job, base64=base64.b64encode(b"changed").decode())

    def test_private_reply_and_streamed_draft_keep_file_private(self):
        job = self.task(private=True)
        with self.store.connect() as db:
            task = dict(db.execute("SELECT * FROM tasks WHERE job=?", (job["id"],)).fetchone())
        self.coordinator.update_reply_draft(task, "Preparing requested report")
        file = self.upload(job)
        self.complete(job, [file["id"]])
        message = self.store.project_view(self.owner, self.project)["messages"][-1]
        self.assertEqual(message["recipient_id"], self.owner)
        self.assertEqual(message["attachments"], [file])
        with self.assertRaises(PermissionError):
            self.files.download(self.viewer, self.project, file["id"])
        self.assertFalse(any(m["attachments"] for m in self.store.project_view(self.viewer, self.project)["messages"]))

    def test_upload_requires_current_device_lease_membership_and_slot(self):
        job = self.task()
        with self.assertRaises(PermissionError):
            self.upload(job, lease_id="wrong")
        with self.assertRaises(ValueError):
            self.upload(job, slot=5)
        with self.assertRaises(ValueError):
            self.upload(job, slot=True)
        pair = self.backend.create_pair(self.viewer, "other-node")
        other_token = secrets.token_urlsafe(32)
        self.backend.device_api("/api/device/enroll", {"pair_code": pair["pair_code"], "device_token": other_token})
        with self.assertRaises(PermissionError):
            self.backend.device_api(
                "/api/device/task-attachment",
                {
                    "job_id": job["id"],
                    "lease_id": job["lease_id"],
                    "slot": 0,
                    "name": "report.pdf",
                    "base64": base64.b64encode(self.content).decode(),
                },
                other_token,
            )
        with patch("agent_harness.attachments.PROJECT_QUOTA", 1):
            with self.assertRaises(ValueError):
                self.upload(job)
        with self.store.connect() as db:
            db.execute("UPDATE pull_jobs SET cancel_requested=1 WHERE id=?", (job["id"],))
        with self.assertRaises(PermissionError):
            self.upload(job)
        with self.store.connect() as db:
            db.execute("UPDATE pull_jobs SET cancel_requested=0 WHERE id=?", (job["id"],))
            db.execute("DELETE FROM project_users WHERE project_id=? AND user_id=?", (self.project, self.owner))
        with self.assertRaises(PermissionError):
            self.upload(job)

    def test_attachment_from_another_task_cannot_be_forwarded(self):
        first = self.task()
        file = self.upload(first)
        self.complete(first, [])
        second = self.task()
        self.complete(second, [file["id"]])
        message = self.store.project_view(self.owner, self.project)["messages"][-1]
        self.assertEqual(message["attachments"], [])
        self.assertIn("附件验证失败", message["body"])

    @unittest.skipUnless(project_files_supported(), "Secure project reader unavailable")
    def test_receiver_retries_lost_upload_ack_without_duplicate_or_path_disclosure(self):
        job = self.task()
        attempts = []

        def transport(_, operation, payload):
            result = self.backend.device_api("/api/device/" + operation, payload, self.token)
            attempts.append(result["id"])
            if len(attempts) == 1:
                raise TimeoutError("lost acknowledgement")
            return result

        connector, receipt = self.connector_receipt(job, transport)
        with self.assertRaises(TimeoutError):
            prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE, APPROVAL_FEATURE])
        prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE])
        answer = json.loads(receipt["result"]["report"])
        self.assertNotIn("attachments", answer)
        self.assertEqual(answer["attachment_ids"], [attempts[0]])
        self.assertEqual(attempts[0], attempts[1])
        prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE])
        self.assertEqual(len(attempts), 2)
        self.assertTrue(read_json(connector.receipt_path(job["id"]))["attachment_delivery_complete"])

    def test_old_hub_gets_clear_error_without_upload_or_local_path(self):
        connector, receipt = self.connector_receipt(self.task())
        prepare_reply(connector, receipt, [])
        answer = json.loads(receipt["result"]["report"])
        self.assertEqual(answer["attachment_ids"], [])
        self.assertNotIn("attachments", answer)
        self.assertIn("托管端需要升级", answer["reply"])

    def test_http_upload_accepts_a_pdf_larger_than_the_normal_device_body_limit(self):
        job = self.task()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.root / "hub"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = {"server": f"http://127.0.0.1:{server.server_port}", "device_token": self.token}
            uploaded = api(
                config,
                "task-attachment",
                {
                    "job_id": job["id"],
                    "lease_id": job["lease_id"],
                    "slot": 0,
                    "name": "report.pdf",
                    "base64": base64.b64encode(self.content).decode(),
                },
            )
            self.assertEqual(uploaded["size"], len(self.content))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_unsupported_reader_never_opens_files(self):
        with (
            patch("agent_harness.agent_attachments.project_files_supported", return_value=False),
            patch("agent_harness.agent_attachments.os.open") as opened,
        ):
            with self.assertRaisesRegex(ValueError, "暂不支持"):
                read_project_file(self.workspace, "report.pdf")
            opened.assert_not_called()

    def test_unsupported_receiver_explains_limit_without_upload_or_path_disclosure(self):
        connector, receipt = self.connector_receipt(self.task())
        with (
            patch("agent_harness.agent_attachments.project_files_supported", return_value=False),
            patch("agent_harness.agent_attachments.read_project_file") as read,
            patch.object(connector, "transport") as upload,
        ):
            prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE])
            read.assert_not_called()
            upload.assert_not_called()
        answer = json.loads(receipt["result"]["report"])
        self.assertEqual(answer["attachment_ids"], [])
        self.assertNotIn("attachments", answer)
        self.assertNotIn(str(self.workspace), receipt["result"]["report"])
        self.assertIn("桌面端手动上传", answer["reply"])
        self.assertTrue(receipt["attachment_delivery_complete"])

    def test_owner_approval_survives_poll_and_uploads_original_file(self):
        job = self.task()
        connector, receipt = self.connector_receipt(job, policy="ask")
        with (
            patch("agent_harness.agent_attachments.project_files_supported", return_value=True),
            patch("agent_harness.agent_attachments.read_project_file", return_value=("report.pdf", self.content)),
        ):
            prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE, APPROVAL_FEATURE])
            self.assertEqual(receipt["phase"], "waiting_attachment_approval")
            account = AccountBackend(self.store, self.backend)
            requests = account._operation(self.owner, "", "attachment-approvals", {})["requests"]
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["manifest"][0]["name"], "report.pdf")
            self.assertEqual(requests[0]["destination"], "项目群聊")
            self.assertEqual(
                self.backend.device_api(
                    "/api/device/task-attachment-request",
                    {"job_id": job["id"], "lease_id": job["lease_id"], "manifest": requests[0]["manifest"]},
                    self.token,
                )["id"],
                requests[0]["id"],
            )
            with self.assertRaises(PermissionError):
                account._operation(self.viewer, "", "attachment-decide", {"id": requests[0]["id"], "approve": True})
            account._operation(self.owner, "", "attachment-decide", {"id": requests[0]["id"], "approve": True})
            decision = self.backend.device_api("/api/device/poll", {}, self.token)["attachment_decisions"][0]
            prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE, APPROVAL_FEATURE], decision)
        answer = json.loads(receipt["result"]["report"])
        self.assertEqual(len(answer["attachment_ids"]), 1)
        self.assertNotIn("attachments", answer)
        self.assertTrue(receipt["attachment_delivery_complete"])

    def test_changed_file_after_approval_is_not_uploaded(self):
        job = self.task()
        connector, receipt = self.connector_receipt(job, policy="ask")
        account = AccountBackend(self.store, self.backend)
        with (
            patch("agent_harness.agent_attachments.project_files_supported", return_value=True),
            patch(
                "agent_harness.agent_attachments.read_project_file",
                side_effect=[("report.pdf", self.content), ("report.pdf", b"changed")],
            ),
        ):
            prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE, APPROVAL_FEATURE])
            approval = account._operation(self.owner, "", "attachment-approvals", {})["requests"][0]
            account._operation(self.owner, "", "attachment-decide", {"id": approval["id"], "approve": True})
            decision = self.backend.device_api("/api/device/poll", {}, self.token)["attachment_decisions"][0]
            prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE, APPROVAL_FEATURE], decision)
        answer = json.loads(receipt["result"]["report"])
        self.assertEqual(answer["attachment_ids"], [])
        self.assertIn("文件内容发生变化", answer["reply"])

    def test_owner_rejection_finishes_without_upload(self):
        job = self.task()
        connector, receipt = self.connector_receipt(job, policy="ask")
        account = AccountBackend(self.store, self.backend)
        with (
            patch("agent_harness.agent_attachments.project_files_supported", return_value=True),
            patch("agent_harness.agent_attachments.read_project_file", return_value=("report.pdf", self.content)),
        ):
            prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE, APPROVAL_FEATURE])
            approval = account._operation(self.owner, "", "attachment-approvals", {})["requests"][0]
            account._operation(self.owner, "", "attachment-decide", {"id": approval["id"], "approve": False})
            decision = self.backend.device_api("/api/device/poll", {}, self.token)["attachment_decisions"][0]
            prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE, APPROVAL_FEATURE], decision)
        answer = json.loads(receipt["result"]["report"])
        self.assertEqual(answer["attachment_ids"], [])
        self.assertIn("拒绝", answer["reply"])

    def test_auto_policy_requires_current_human_file_request(self):
        job = self.task()
        connector, receipt = self.connector_receipt(job, policy="auto")
        config = read_json(connector.root / "jobs/local/request.json")
        config["prompt"] = json.dumps(
            {
                "objective": json.dumps(
                    {
                        "visible_messages": [
                            {"sender_type": "human", "recipient_id": None, "body": "请发送 report.pdf"}
                        ],
                        "task": {},
                    }
                )
            }
        )
        write_json(connector.root / "jobs/local/request.json", config)
        with (
            patch("agent_harness.agent_attachments.project_files_supported", return_value=True),
            patch("agent_harness.agent_attachments.read_project_file", return_value=("report.pdf", self.content)),
        ):
            prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE])
        self.assertEqual(receipt["phase"], "terminal")
        self.assertEqual(len(json.loads(receipt["result"]["report"])["attachment_ids"]), 1)

    def test_auto_policy_accepts_file_request_in_private_chat(self):
        job = self.task(private=True)
        connector, receipt = self.connector_receipt(job, policy="auto")
        config = read_json(connector.root / "jobs/local/request.json")
        config["prompt"] = json.dumps(
            {
                "objective": json.dumps(
                    {
                        "task": {"private_turn": True, "reply_to": self.owner},
                        "visible_messages": [
                            {
                                "sender_type": "human",
                                "sender_id": self.owner,
                                "recipient_id": self.agent["id"],
                                "body": "请发送 report.pdf",
                            }
                        ],
                    }
                )
            }
        )
        write_json(connector.root / "jobs/local/request.json", config)
        with (
            patch("agent_harness.agent_attachments.project_files_supported", return_value=True),
            patch("agent_harness.agent_attachments.read_project_file", return_value=("report.pdf", self.content)),
        ):
            prepare_reply(connector, receipt, [AGENT_ATTACHMENT_FEATURE])
        self.assertEqual(receipt["phase"], "terminal")
        self.assertEqual(len(json.loads(receipt["result"]["report"])["attachment_ids"]), 1)

    @unittest.skipUnless(os.name == "nt", "Windows reparse points only")
    def test_windows_directory_replacement_with_junction_is_blocked(self):
        self.assertTrue(project_files_supported())
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.pdf").write_bytes(b"private outside content")
        link = self.workspace / "replaced"
        link.mkdir()
        link.rmdir()
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
        try:
            with patch("agent_harness.agent_attachments.os.open") as opened:
                with self.assertRaises(ValueError):
                    read_project_file(self.workspace, "replaced/secret.pdf")
                opened.assert_not_called()
        finally:
            link.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows symlinks only")
    def test_windows_file_replacement_with_symlink_is_blocked(self):
        outside = self.root / "secret.pdf"
        outside.write_bytes(b"private outside content")
        link = self.workspace / "replaced.pdf"
        link.write_bytes(b"original content")
        link.unlink()
        try:
            link.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows account cannot create symbolic links")
            raise
        with patch("agent_harness.agent_attachments.os.open") as opened:
            with self.assertRaises(ValueError):
                read_project_file(self.workspace, "replaced.pdf")
            opened.assert_not_called()

    @unittest.skipUnless(os.name != "nt" and project_files_supported(), "POSIX descriptor-relative reader only")
    def test_directory_replaced_after_open_cannot_redirect_the_read(self):
        reports = self.workspace / "reports"
        reports.mkdir()
        (reports / "report.pdf").write_bytes(b"requested report")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "report.pdf").write_bytes(b"private outside content")
        original_open = os.open

        def replace_before_file_open(path, flags, *args, **kwargs):
            if path == "report.pdf":
                reports.rename(self.workspace / "original-reports")
                reports.symlink_to(outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        with (
            patch("agent_harness.agent_attachments.project_files_supported", return_value=True),
            patch("agent_harness.agent_attachments.os.open", side_effect=replace_before_file_open),
        ):
            self.assertEqual(
                read_project_file(self.workspace, "reports/report.pdf"), ("report.pdf", b"requested report")
            )

    @unittest.skipUnless(os.name != "nt" and project_files_supported(), "POSIX descriptor-relative reader only")
    def test_file_replaced_with_symlink_immediately_before_open_is_blocked(self):
        outside = self.root / "secret.pdf"
        outside.write_bytes(b"private outside content")
        original_open = os.open

        def replace_before_file_open(path, flags, *args, **kwargs):
            if path == "report.pdf":
                target = self.workspace / "report.pdf"
                target.unlink()
                target.symlink_to(outside)
            return original_open(path, flags, *args, **kwargs)

        with (
            patch("agent_harness.agent_attachments.project_files_supported", return_value=True),
            patch("agent_harness.agent_attachments.os.open", side_effect=replace_before_file_open),
        ):
            with self.assertRaises(OSError):
                read_project_file(self.workspace, "report.pdf")

    @unittest.skipUnless(os.name != "nt" and project_files_supported(), "POSIX descriptor-relative reader only")
    def test_local_reader_rejects_outside_hidden_symlink_fifo_and_oversize_files(self):
        self.assertEqual(read_project_file(self.workspace, "report.pdf"), ("report.pdf", self.content))
        for name in ("../outside.pdf", str(self.root / "outside.pdf"), ".env"):
            with self.assertRaises((ValueError, OSError)):
                read_project_file(self.workspace, name)
        if hasattr(os, "symlink"):
            link = self.workspace / "link.pdf"
            try:
                link.symlink_to(self.workspace / "report.pdf")
            except OSError:
                pass
            else:
                with self.assertRaises((ValueError, OSError)):
                    read_project_file(self.workspace, "link.pdf")
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.workspace / "pipe")
            with self.assertRaises(ValueError):
                read_project_file(self.workspace, "pipe")
        with (self.workspace / "large.pdf").open("wb") as stream:
            stream.truncate(MAX_FILE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "最大 5 MB"):
            read_project_file(self.workspace, "large.pdf")


if __name__ == "__main__":
    unittest.main()
