"""Consent, ownership and durable delivery checks for the pull-only broker."""

import json
import hashlib
import secrets
import tempfile
import threading
import time
import unittest

from agent_harness.broker import PullBackend
from agent_harness.collaboration_store import Store, uid
from agent_harness.file_transfers import FileTransfers
from tests.helpers import provision_account


class PullBrokerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.owner = provision_account(self.store, "owner", bootstrap=True)["id"]
        self.project = self.store.project_create(self.owner, "Shared project")["id"]
        self.backend = PullBackend(self.store)

    def enroll(self, node="worker", owner=None):
        pair = self.backend.create_pair(owner or self.owner, node)
        token = secrets.token_urlsafe(32)
        result = self.backend.device_api(
            "/api/device/enroll",
            {"pair_code": pair["pair_code"], "device_token": token},
        )
        self.assertEqual(result["node"], node)
        return token, pair

    def advertise(self, token, node="worker", project=None, backend="workspace", **extra):
        binding = dict(
            project_id=project or self.project,
            backend_project=backend,
            name="Peer",
            model="gpt-5.6-sol",
            effort="medium",
            timeout=120,
            enabled=True,
            stages=["discuss"],
            templates=["train"],
        )
        binding.update(extra)
        self.backend.device_api("/api/device/poll", {"bindings": [binding]}, token)
        with self.store.connect() as db:
            return dict(
                db.execute(
                    "SELECT * FROM agents WHERE node=? AND backend_project=?",
                    (node, backend),
                ).fetchone()
            )

    def submit(self, agent, job="job-fixed", **extra):
        return self.backend.submit(
            agent["backend_project"],
            json.dumps({"identity": {"id": agent["id"]}}),
            extra.get("stage", "discuss"),
            "permitted files",
            "success",
            "check",
            job_id=job,
            session_channel="group",
        )

    def claim(self, token):
        return self.backend.device_api("/api/device/poll", {}, token)["jobs"][0]

    def complete(self, token, job, report="done"):
        return self.backend.device_api(
            "/api/device/report",
            dict(
                job_id=job["id"],
                lease_id=job["lease_id"],
                state="needs_review",
                result={"report": report},
            ),
            token,
        )

    def test_idle_long_poll_wakes_when_a_job_is_submitted(self):
        token, _ = self.enroll()
        agent = self.advertise(token)
        result = {}

        def wait_for_job():
            result.update(
                self.backend.device_api(
                    "/api/device/poll",
                    {"wait_seconds": 2},
                    token,
                )
            )

        waiter = threading.Thread(target=wait_for_job)
        waiter.start()
        time.sleep(0.2)
        self.submit(agent)
        waiter.join(3)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(result["jobs"][0]["agent_id"], agent["id"])

    def test_long_poll_timeout_is_bounded(self):
        token, _ = self.enroll()
        with self.assertRaisesRegex(ValueError, "wait_seconds"):
            self.backend.device_api("/api/device/poll", {"wait_seconds": 11}, token)

    def test_pair_is_one_time_and_token_not_public(self):
        token, pair = self.enroll()
        with self.assertRaises(PermissionError):
            self.backend.device_api(
                "/api/device/enroll",
                {"pair_code": pair["pair_code"], "device_token": token},
            )
        with self.assertRaises(PermissionError):
            self.backend.device_api("/api/device/poll", {}, "x" * 32)
        public = self.backend.snapshot_nodes(self.owner)
        self.assertEqual(public[0]["paired"], 1)
        self.assertNotIn(token, json.dumps(public))
        self.assertNotIn("token_hash", json.dumps(public))

    def test_device_transfer_api_is_bound_to_exact_recipient_node(self):
        receiver_token, _ = self.enroll("receiver")
        sender_token, _ = self.enroll("sender")
        content = b"headless linux transfer"
        files = FileTransfers(self.store)
        transfer = files.request(
            self.owner,
            "sender",
            self.owner,
            "receiver",
            "payload.bin",
            len(content),
            hashlib.sha256(content).hexdigest(),
        )
        upload = files.upload_start(self.owner, "sender", transfer["id"])
        files.upload_chunk(
            upload["upload_token"],
            transfer["id"],
            0,
            len(content),
            content,
            hashlib.sha256(content).hexdigest(),
        )
        files.finish(self.owner, "sender", transfer["id"])

        listing = self.backend.device_api("/api/device/transfers", {}, receiver_token)
        self.assertEqual([item["id"] for item in listing["transfers"]], [transfer["id"]])
        self.assertEqual(
            self.backend.device_api("/api/device/transfers", {}, sender_token)["transfers"],
            [],
        )
        with self.assertRaises(PermissionError):
            self.backend.device_api(
                "/api/device/transfer-download-ticket",
                {"id": transfer["id"]},
                sender_token,
            )
        ticket = self.backend.device_api(
            "/api/device/transfer-download-ticket",
            {"id": transfer["id"]},
            receiver_token,
        )
        self.assertIn("download_token", ticket)
        _, start, end, downloaded = files.download_chunk(
            ticket["download_token"],
            transfer["id"],
            0,
            len(content) - 1,
        )
        self.assertEqual((start, end, downloaded), (0, len(content) - 1, content))
        completed = self.backend.device_api(
            "/api/device/transfer-complete",
            {"id": transfer["id"]},
            receiver_token,
        )
        self.assertEqual(completed["state"], "completed")

    def test_health_heartbeat_is_allowlisted_and_available_to_owner(self):
        token, pair = self.enroll()
        payload = {
            "schema_version": 1,
            "generated_at": time.time(),
            "overall": "unavailable",
            "components": [
                {
                    "id": "codex",
                    "state": "unavailable",
                    "code": "CLI_NOT_LOGGED_IN",
                    "message": "login token=/secret",
                    "retryable": False,
                    "updated_at": time.time(),
                    "private_field": "must drop",
                }
            ],
            "secret": "must drop",
        }
        self.backend.device_api("/api/device/poll", {"health": payload}, token)
        config = self.store.root / "nodes" / (pair["node"] + ".json")
        saved = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(saved["health"]["overall"], "unavailable")
        self.assertNotIn("secret", json.dumps(saved))
        self.assertNotIn("private_field", json.dumps(saved))
        self.assertNotIn("/secret", json.dumps(saved))

    def test_ownership_project_and_report_isolation(self):
        token, _ = self.enroll()
        invite = self.store.invite(self.owner, self.project)["invite"]
        other = provision_account(self.store, "other", invite=invite)["id"]
        with self.assertRaises(PermissionError):
            self.backend.create_pair(other, "worker")
        other_token, _ = self.enroll("other-worker", other)
        # Even a manually crafted cross-owner project must preserve agent and
        # report isolation.
        other_project = uid("project")
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO projects(id,name,owner_id) VALUES(?,?,?)",
                (other_project, "Private project", other),
            )
            db.execute("INSERT INTO project_users VALUES(?,?)", (other_project, other))
        with self.assertRaises(PermissionError):
            self.advertise(token, project=other_project)
        agent = self.advertise(token)
        self.submit(agent)
        job = self.claim(token)
        self.assertEqual(self.backend.device_api("/api/device/poll", {}, other_token)["jobs"], [])
        with self.assertRaises(PermissionError):
            self.complete(other_token, job)

    def test_fresh_schema_contains_current_fields_without_migration_metadata(self):
        with self.store.connect() as db:
            tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("pull_meta", tables)
            self.assertNotIn(
                "budget_remaining",
                {row["name"] for row in db.execute("PRAGMA table_info(projects)")},
            )
            self.assertTrue({"configuring"} <= {row["name"] for row in db.execute("PRAGMA table_info(agents)")})
            self.assertTrue(
                {
                    "mention_agent_id",
                    "conversation_id",
                    "client_message_id",
                    "sender_client",
                }
                <= {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
            )
            self.assertTrue({"cancel_requested"} <= {row["name"] for row in db.execute("PRAGMA table_info(tasks)")})
            self.assertTrue(
                {"reply_to", "cancel_requested"} <= {row["name"] for row in db.execute("PRAGMA table_info(executions)")}
            )

    def test_repeated_poll_and_submit_keep_single_lease(self):
        token, _ = self.enroll()
        agent = self.advertise(token)
        first = self.submit(agent)
        self.assertEqual(self.submit(agent), first)
        one = self.claim(token)
        two = self.claim(token)
        self.assertEqual(one, two)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pull_jobs").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            self.backend.submit(
                "workspace",
                json.dumps({"identity": {"id": agent["id"]}, "changed": True}),
                "discuss",
                "",
                "a",
                "v",
                job_id=first["job"],
            )

    def test_stale_report_and_terminal_overwrite_rejected(self):
        token, _ = self.enroll()
        agent = self.advertise(token)
        self.submit(agent)
        job = self.claim(token)
        stale = dict(job_id=job["id"], lease_id="wrong", state="failed", result={})
        with self.assertRaises(PermissionError):
            self.backend.device_api("/api/device/report", stale, token)
        self.assertTrue(self.complete(token, job)["ok"])
        self.assertTrue(self.complete(token, job)["duplicate"])
        with self.assertRaises(ValueError):
            self.complete(token, job, "overwrite")
        self.assertEqual(self.backend.status(job["id"])["report"], "done")
        self.assertEqual(self.backend.device_api("/api/device/poll", {}, token)["jobs"], [])

    def test_repair_active_job_denied_without_rotating_old_token(self):
        token, _ = self.enroll()
        agent = self.advertise(token)
        self.submit(agent)
        job = self.claim(token)
        pair = self.backend.create_pair(self.owner, "worker")
        replacement = secrets.token_urlsafe(32)
        request = {"pair_code": pair["pair_code"], "device_token": replacement}
        with self.assertRaises(PermissionError):
            self.backend.device_api("/api/device/enroll", request)
        self.assertEqual(self.claim(token)["lease_id"], job["lease_id"])
        self.complete(token, job)
        self.backend.device_api("/api/device/enroll", request)
        with self.assertRaises(PermissionError):
            self.backend.device_api("/api/device/poll", {}, token)

    def test_local_stage_permission_and_central_settings_rejected(self):
        token, _ = self.enroll()
        agent = self.advertise(token)
        with self.assertRaises(PermissionError):
            self.submit(agent, stage="implement")
        with self.assertRaises(PermissionError):
            self.backend.configure("workspace", model="another-model")
        with self.assertRaises(PermissionError):
            self.backend.for_node("worker").rpc(
                {
                    "action": "execution_templates",
                    "project": "workspace",
                    "templates": {},
                }
            )
        self.assertEqual(
            self.backend.for_node("worker").rpc({"action": "execution_templates", "project": "workspace"})["templates"],
            ["train"],
        )
        self.backend.device_api("/api/device/poll", {"bindings": []}, token)
        with self.assertRaises(PermissionError):
            self.submit(agent)

    def test_cancel_is_intent_until_local_confirmation(self):
        token, _ = self.enroll()
        agent = self.advertise(token)
        self.submit(agent)
        job = self.claim(token)
        self.assertEqual(
            self.backend.rpc({"action": "cancel", "job": job["id"]})["status"],
            "running",
        )
        self.assertIn(
            job["id"],
            self.backend.device_api("/api/device/poll", {}, token)["cancellations"],
        )
        self.backend.device_api(
            "/api/device/report",
            dict(job_id=job["id"], lease_id=job["lease_id"], state="cancelled"),
            token,
        )
        self.assertEqual(self.backend.status(job["id"])["status"], "cancelled")

    def test_model_audit_metadata_round_trips_and_is_validated(self):
        token, _ = self.enroll()
        agent = self.advertise(token)
        self.submit(agent)
        job = self.claim(token)
        result = {
            "report": "done",
            "requested_model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "effective_model": "gpt-5.6-sol",
            "model_verified": True,
        }
        self.backend.device_api(
            "/api/device/report",
            dict(
                job_id=job["id"],
                lease_id=job["lease_id"],
                state="needs_review",
                result=result,
            ),
            token,
        )
        status = self.backend.status(job["id"])
        for key, value in result.items():
            self.assertEqual(status[key], value)
        token, _ = self.enroll("second")
        other = self.advertise(token, node="second", backend="other")
        self.submit(other, job="job-invalid")
        claimed = self.claim(token)
        with self.assertRaises(ValueError):
            self.backend.device_api(
                "/api/device/report",
                dict(
                    job_id=claimed["id"],
                    lease_id=claimed["lease_id"],
                    state="needs_review",
                    result={"model_verified": "yes"},
                ),
                token,
            )


if __name__ == "__main__":
    unittest.main()
