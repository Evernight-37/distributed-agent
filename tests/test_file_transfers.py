import hashlib
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from agent_harness.collaboration_store import Store
from agent_harness.file_transfers import FileTransfers, MAX_ACTIVE_UPLOADS_PER_ACCOUNT
from agent_harness.panel import handler_for


def node_for(public_key):
    return "device-" + hashlib.sha256(public_key.encode()).hexdigest()[:24]


class FileTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.files = FileTransfers(self.store)
        self.a = "account-a"
        self.b = "account-b"
        self.a_node = node_for("public-a")
        self.a_other = node_for("public-a-other")
        self.b_node = node_for("public-b")
        with self.store.connect() as db:
            db.executemany("INSERT INTO users VALUES(?,?)", [(self.a, "Alice"), (self.b, "Bob")])
            db.executemany(
                "INSERT INTO account_devices VALUES(?,?,?,?,0)",
                [
                    ("public-a", self.a, "Alice PC", "[]"),
                    ("public-a-other", self.a, "Alice Server", "[]"),
                    ("public-b", self.b, "Bob PC", "[]"),
                ],
            )
            db.executemany(
                "INSERT INTO nodes VALUES(?,?)",
                [(self.a_node, self.a), (self.a_other, self.a), (self.b_node, self.b)],
            )
            db.execute("INSERT INTO projects(id,name,owner_id) VALUES('project','P',?)", (self.a,))
            db.executemany(
                "INSERT INTO project_users VALUES('project',?)",
                [(self.a,), (self.b,)],
            )

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, recipient=None, node=None, content=b"hello"):
        recipient = recipient or self.b
        return self.files.request(
            self.a,
            self.a_node,
            recipient,
            node,
            "sample.bin",
            len(content),
            hashlib.sha256(content).hexdigest(),
        )

    def test_other_user_requires_confirmation_then_round_trips(self):
        content = b"hello relay"
        transfer = self.request(content=content)
        self.assertEqual(transfer["state"], "requested")
        accepted = self.files.decide(self.b, self.b_node, transfer["id"], True)
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(accepted["recipient_node"], self.b_node)

        upload = self.files.upload_start(self.a, self.a_node, transfer["id"])
        self.files.upload_chunk(
            upload["upload_token"],
            transfer["id"],
            0,
            len(content),
            content,
            hashlib.sha256(content).hexdigest(),
        )
        ready = self.files.finish(self.a, self.a_node, transfer["id"])
        self.assertEqual(ready["state"], "ready")

        ticket = self.files.download_ticket(self.b, self.b_node, transfer["id"])
        _, start, end, received = self.files.download_chunk(
            ticket["download_token"], transfer["id"], 0, len(content) - 1
        )
        self.assertEqual((start, end, received), (0, len(content) - 1, content))
        done = self.files.complete(self.b, self.b_node, transfer["id"])
        self.assertEqual(done["state"], "completed")
        self.assertFalse((self.root / "file-transfers" / (transfer["id"] + ".bin")).exists())

    def test_approval_can_assign_another_node_owned_by_recipient(self):
        b_other = node_for("public-b-other")
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO account_devices VALUES(?,?,?,?,0)",
                ("public-b-other", self.b, "Bob Server", "[]"),
            )
            db.execute("INSERT INTO nodes VALUES(?,?)", (b_other, self.b))
        transfer = self.request()
        accepted = self.files.decide(
            self.b,
            self.b_node,
            transfer["id"],
            True,
            recipient_node=b_other,
        )
        self.assertEqual(accepted["recipient_node"], b_other)

        other_account_node = self.a_other
        another = self.request()
        with self.assertRaisesRegex(PermissionError, "不属于"):
            self.files.decide(
                self.b,
                self.b_node,
                another["id"],
                True,
                recipient_node=other_account_node,
            )

    def test_same_account_other_device_is_accepted_without_confirmation(self):
        transfer = self.request(recipient=self.a, node=self.a_other)
        self.assertEqual(transfer["state"], "accepted")
        with self.assertRaisesRegex(ValueError, "不需要人工确认"):
            self.files.decide(self.a, self.a_other, transfer["id"], True)

    def test_recovers_crash_between_upload_rename_and_database_commit(self):
        content = b"recover upload"
        transfer = self.request(recipient=self.a, node=self.a_other, content=content)
        session = self.files.upload_start(self.a, self.a_node, transfer["id"])
        self.files.upload_chunk(
            session["upload_token"],
            transfer["id"],
            0,
            len(content),
            content,
            hashlib.sha256(content).hexdigest(),
        )
        os.replace(self.files._part(transfer["id"]), self.files._file(transfer["id"]))
        self.files.recover_interrupted_commits()
        self.assertEqual(self.files.status(self.a, transfer["id"])["state"], "ready")

    def test_recovers_crash_after_ready_file_moved_to_trash(self):
        content = b"recover download"
        transfer = self.request(recipient=self.a, node=self.a_other, content=content)
        session = self.files.upload_start(self.a, self.a_node, transfer["id"])
        self.files.upload_chunk(
            session["upload_token"],
            transfer["id"],
            0,
            len(content),
            content,
            hashlib.sha256(content).hexdigest(),
        )
        self.files.finish(self.a, self.a_node, transfer["id"])
        crashed = self.files.trash / (transfer["id"] + ".crashed")
        os.replace(self.files._file(transfer["id"]), crashed)
        self.files.recover_interrupted_commits()
        self.assertTrue(self.files._file(transfer["id"]).is_file())
        self.assertFalse(crashed.exists())

    def test_oversized_partial_upload_restarts_from_zero(self):
        content = b"small"
        transfer = self.request(recipient=self.a, node=self.a_other, content=content)
        self.files.upload_start(self.a, self.a_node, transfer["id"])
        self.files._part(transfer["id"]).write_bytes(content + b"corrupt")
        restarted = self.files.upload_start(self.a, self.a_node, transfer["id"])
        self.assertEqual(restarted["received_bytes"], 0)
        self.assertFalse(self.files._part(transfer["id"]).exists())

    def test_request_has_no_fixed_total_size_cap(self):
        size = 8 * 1024**4
        transfer = self.files.request(
            self.a,
            self.a_node,
            self.a,
            self.a_other,
            "huge.dataset",
            size,
            "0" * 64,
        )
        self.assertEqual(transfer["size"], size)
        self.assertEqual(transfer["state"], "accepted")

    def test_transfer_is_not_project_owned_after_trust_is_established(self):
        transfer = self.request()
        with self.store.connect() as db:
            db.execute("DELETE FROM project_users WHERE user_id=?", (self.b,))
            columns = [row[1] for row in db.execute("PRAGMA table_info(file_transfers)")]
        self.assertNotIn("project_id", columns)
        accepted = self.files.decide(self.b, self.b_node, transfer["id"], True)
        self.assertEqual(accepted["state"], "accepted")

    def test_unrelated_accounts_are_not_enumerated_or_accepted(self):
        c = "account-c"
        c_node = node_for("public-c")
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(?,?)", (c, "Carol"))
            db.execute("INSERT INTO account_devices VALUES(?,?,?,?,0)", ("public-c", c, "Carol PC", "[]"))
            db.execute("INSERT INTO nodes VALUES(?,?)", (c_node, c))
        peers = self.files.recipients(self.a)
        self.assertEqual([item["id"] for item in peers["contacts"]], [self.b])
        with self.assertRaises(PermissionError):
            self.files.request(self.a, self.a_node, c, None, "x", 1, "0" * 64)

    def test_upload_token_and_download_are_bound_to_exact_devices(self):
        transfer = self.request()
        self.files.decide(self.b, self.b_node, transfer["id"], True)
        with self.assertRaises(PermissionError):
            self.files.upload_start(self.a, self.a_other, transfer["id"])
        with self.assertRaises(PermissionError):
            self.files.download_ticket(self.b, self.a_other, transfer["id"])

    def test_at_most_three_active_uploads_per_sender(self):
        transfers = []
        for index in range(MAX_ACTIVE_UPLOADS_PER_ACCOUNT + 1):
            content = bytes([index + 1])
            item = self.files.request(
                self.a,
                self.a_node,
                self.b,
                None,
                f"{index}.bin",
                1,
                hashlib.sha256(content).hexdigest(),
            )
            self.files.decide(self.b, self.b_node, item["id"], True)
            transfers.append(item)
        for item in transfers[:MAX_ACTIVE_UPLOADS_PER_ACCOUNT]:
            self.files.upload_start(self.a, self.a_node, item["id"])
        with self.assertRaisesRegex(RuntimeError, "同时发送"):
            self.files.upload_start(self.a, self.a_node, transfers[-1]["id"])

    def test_service_staging_rejects_symbolic_links(self):
        content = b"x"
        transfer = self.request(content=content)
        self.files.decide(self.b, self.b_node, transfer["id"], True)
        upload = self.files.upload_start(self.a, self.a_node, transfer["id"])
        outside = self.root / "outside.bin"
        outside.write_bytes(b"untouched")
        try:
            os.symlink(outside, self.files._part(transfer["id"]))
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        with self.assertRaisesRegex(RuntimeError, "不是普通文件"):
            self.files.upload_chunk(
                upload["upload_token"], transfer["id"], 0, 1, content, hashlib.sha256(content).hexdigest()
            )
        self.assertEqual(outside.read_bytes(), b"untouched")

    def test_finish_and_cancel_are_serialized_without_orphans(self):
        content = b"race"
        transfer = self.request(content=content)
        self.files.decide(self.b, self.b_node, transfer["id"], True)
        upload = self.files.upload_start(self.a, self.a_node, transfer["id"])
        self.files.upload_chunk(
            upload["upload_token"],
            transfer["id"],
            0,
            len(content),
            content,
            hashlib.sha256(content).hexdigest(),
        )
        barrier = threading.Barrier(3)
        outcomes = []

        def run(call):
            barrier.wait()
            try:
                call()
                outcomes.append("ok")
            except (ValueError, PermissionError, RuntimeError):
                outcomes.append("rejected")

        workers = [
            threading.Thread(target=run, args=(lambda: self.files.finish(self.a, self.a_node, transfer["id"]),)),
            threading.Thread(target=run, args=(lambda: self.files.cancel(self.a, transfer["id"]),)),
        ]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=2)
        self.assertEqual(len(outcomes), 2)
        with self.store.connect() as db:
            state = db.execute("SELECT state FROM file_transfers WHERE id=?", (transfer["id"],)).fetchone()[0]
        final = self.files._file(transfer["id"])
        part = self.files._part(transfer["id"])
        if state == "ready":
            self.assertTrue(final.is_file())
            self.assertFalse(part.exists())
        else:
            self.assertEqual(state, "cancelled")
            self.assertFalse(final.exists())
            self.assertFalse(part.exists())


class FileTransferHttpTests(FileTransferTests):
    def test_binary_data_plane_uses_transfer_paths_and_range(self):
        content = b"http relay"
        transfer = self.request(content=content)
        self.files.decide(self.b, self.b_node, transfer["id"], True)
        upload = self.files.upload_start(self.a, self.a_node, transfer["id"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.root))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            request = urllib.request.Request(
                origin + "/api/transfers/uploads/" + transfer["id"],
                data=content,
                method="PUT",
                headers={
                    "Authorization": "Bearer " + upload["upload_token"],
                    "Content-Range": f"bytes 0-{len(content) - 1}/{len(content)}",
                    "X-Chunk-SHA256": hashlib.sha256(content).hexdigest(),
                    "Content-Type": "application/octet-stream",
                },
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
            self.files.finish(self.a, self.a_node, transfer["id"])
            ticket = self.files.download_ticket(self.b, self.b_node, transfer["id"])
            request = urllib.request.Request(
                origin + "/api/transfers/" + transfer["id"] + "/content",
                headers={
                    "Authorization": "Bearer " + ticket["download_token"],
                    "Range": f"bytes=0-{len(content) - 1}",
                },
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), content)
                self.assertEqual(response.headers["Content-Range"], f"bytes 0-{len(content) - 1}/{len(content)}")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
