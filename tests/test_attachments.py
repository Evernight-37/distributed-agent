import base64
import tempfile
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_harness.attachments import Attachments, MAX_FILE_BYTES
from agent_harness.collaboration_store import Store
from agent_harness.app_bridge import AppBridge
from agent_harness.panel import handler_for


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.files = Attachments(self.store)
        with self.store.connect() as db:
            for user in ("a", "b", "c", "outsider"):
                db.execute("INSERT INTO users(id,username) VALUES(?,?)", (user, user))
            db.execute("INSERT INTO projects(id,name,owner_id) VALUES('p','project','a')")
            for user in ("a", "b", "c"):
                db.execute("INSERT INTO project_users VALUES('p',?)", (user,))

    def tearDown(self):
        self.directory.cleanup()

    def upload(self, user="a", name="result.txt", data=b"hello"):
        return self.files.upload(user, "p", name, base64.b64encode(data).decode())

    def test_group_attachment_download_and_metadata_round_trip(self):
        file = self.upload(name="../../result.txt")
        result = self.store.message("a", "p", "file", attachment_ids=[file["id"]])
        view = self.store.project_view("b", "p")
        self.assertEqual(view["messages"][-1]["attachments"], [file])
        self.assertNotIn("base64", file)
        self.assertNotIn("/", file["name"])
        self.assertEqual(
            base64.b64decode(self.files.download("b", "p", file["id"])["base64"]),
            b"hello",
        )
        self.assertIsInstance(result["id"], int)
        with self.assertRaises(PermissionError):
            self.files.download("outsider", "p", file["id"])

    def test_private_attachment_cannot_be_read_by_other_project_member(self):
        file = self.upload()
        with self.assertRaises(PermissionError):
            self.files.download("b", "p", file["id"])
        self.store.message("a", "p", "private", recipient="b", attachment_ids=[file["id"]])
        self.assertEqual(self.files.download("b", "p", file["id"])["size"], 5)
        with self.assertRaises(PermissionError):
            self.files.download("c", "p", file["id"])

    def test_foreign_attachment_cannot_be_sent_and_failed_message_rolls_back(self):
        file = self.upload("b")
        with self.assertRaises(PermissionError):
            self.store.message("a", "p", "forged", attachment_ids=[file["id"]])
        self.assertEqual(self.store.project_view("a", "p")["messages"], [])
        self.assertEqual(self.files.download("b", "p", file["id"])["size"], 5)

    def test_retry_does_not_duplicate_attachment_and_reuse_is_rejected(self):
        file = self.upload()
        token = str(uuid.uuid4())
        first = self.store.message("a", "p", "same", client_message_id=token, attachment_ids=[file["id"]])
        second = self.store.message("a", "p", "same", client_message_id=token, attachment_ids=[file["id"]])
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])
        with self.assertRaises(ValueError):
            self.store.message("a", "p", "same", client_message_id=token)
        with self.assertRaises(PermissionError):
            self.store.message("a", "p", "reuse", attachment_ids=[file["id"]])

    def test_limits_and_html_is_never_an_inline_image(self):
        file = self.upload(name="fake.png", data=b"<html><script>alert(1)</script>")
        self.assertEqual(file["mime"], "application/octet-stream")
        with self.assertRaises(ValueError):
            self.upload(data=b"x" * (MAX_FILE_BYTES + 1))
        with self.assertRaises(ValueError):
            self.files.upload("a", "p", "bad", "###")
        with self.assertRaises(ValueError):
            self.store.message("a", "p", "many", attachment_ids=[file["id"]] * 6)
        with patch("agent_harness.attachments.PROJECT_QUOTA", 1):
            with self.assertRaises(ValueError):
                self.upload()

    def test_member_removal_revokes_download_and_old_staged_files_expire(self):
        file = self.upload()
        self.store.message("a", "p", "file", attachment_ids=[file["id"]])
        with self.store.connect() as db:
            db.execute("DELETE FROM project_users WHERE project_id='p' AND user_id='b'")
        with self.assertRaises(PermissionError):
            self.files.download("b", "p", file["id"])
        pending = self.upload()
        with patch("agent_harness.attachments.time.time", return_value=99999999999):
            self.upload()
        with self.assertRaises(PermissionError):
            self.files.download("a", "p", pending["id"])
        self.assertEqual(self.files.download("a", "p", file["id"])["size"], 5)


class AttachmentHttpTests(unittest.TestCase):
    def test_large_signed_upload_round_trip_and_native_save(self):
        class QuietBridge(AppBridge):
            def _enroll(self, server=None):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(root / "service"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                bridge = QuietBridge(root / "app")
                url = "http://127.0.0.1:" + str(server.server_port)
                result = bridge.invoke("register", {"username": "Owner", "password": "", "server": url})
                self.assertTrue(result["ok"], result)
                store = Store(root / "service")
                store.claim_local_host(bridge.identity["account_id"])
                project = store.project_create(bridge.identity["account_id"], "Attachments")["id"]
                content = b"example-file\n" * 50000
                file = bridge.invoke(
                    "attachment-upload",
                    {
                        "project": project,
                        "name": "report.txt",
                        "base64": base64.b64encode(content).decode(),
                    },
                )
                self.assertTrue(file["ok"], file)
                metadata = file["result"]
                sent = bridge.invoke(
                    "message",
                    {
                        "project": project,
                        "body": "file",
                        "attachment_ids": [metadata["id"]],
                    },
                )
                self.assertTrue(sent["ok"], sent)
                bridge.window = SimpleNamespace(create_file_dialog=lambda *a, **kw: str(root / "saved.txt"))
                with patch.dict(
                    "sys.modules",
                    {"webview": SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save"))},
                ):
                    result = bridge.invoke("attachment-save", {"project": project, "id": metadata["id"]})
                self.assertTrue(result["result"]["saved"], result)
                self.assertEqual((root / "saved.txt").read_bytes(), content)
                bridge._rpc("pair-local", {"node": bridge.profile["node"]})
                queued = bridge._owner(
                    "create",
                    bridge.profile["node"],
                    {"id": "employee-http", "name": "Test"},
                )
                status = bridge._rpc("command-status", {"id": queued["id"]})
                self.assertEqual(status["state"], "queued")
                self.assertIsNone(status["result"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
