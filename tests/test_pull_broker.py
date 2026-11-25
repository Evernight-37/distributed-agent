"""Consent, ownership and durable delivery checks for the pull-only broker."""

import json
import secrets
import tempfile
import threading
import time
import unittest

from agent_harness.broker import PullBackend
from agent_harness.collaboration_store import Store, uid
from agent_harness.coordinator import Coordinator
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

    def create_task(self, agent, delegate=None):
        return self.store.task(
            self.owner,
            {
                "project": self.project,
                "agent": agent["id"],
                "title": "Check status",
                "instructions": "Explain status",
                "acceptance": "Known result",
                "validation": "Review",
            },
            delegate=delegate,
        )["id"]

    def test_unknown_task_stays_active_until_cancelled_by_the_receiver(self):
        token, _ = self.enroll()
        agent = self.advertise(token)
        task = self.create_task(agent)
        coordinator = Coordinator(self.store, self.backend)
        coordinator.dispatch(task)
        job = self.claim(token)
        report = {"job_id": job["id"], "lease_id": job["lease_id"]}
        self.backend.device_api(
            "/api/device/report", {**report, "state": "running", "result": {"error": "Unknown job"}}, token
        )
        coordinator.collect(task)
        row = self.store.project_view(self.owner, self.project)["tasks"][0]
        self.assertEqual((row["state"], row["display_status"]), ("unknown", "待核对"))
        self.assertTrue(row["can_cancel"])
        self.assertEqual(row["local_recovery"], {"node": "worker", "job_id": job["id"]})
        delegate_row = self.store.project_view(self.owner, self.project, delegate="delegate")["tasks"][0]
        self.assertNotIn("local_recovery", delegate_row)
        invite = self.store.invite(self.owner, self.project)["invite"]
        peer = provision_account(self.store, "observer", invite=invite)["id"]
        self.assertNotIn("local_recovery", self.store.project_view(peer, self.project)["tasks"][0])
        with self.assertRaisesRegex(ValueError, "活动任务"):
            self.create_task(agent)
        self.backend.device_api("/api/device/report", {**report, "state": "running"}, token)
        coordinator.collect(task)
        self.assertEqual(self.store.project_view(self.owner, self.project)["tasks"][0]["state"], "running")
        self.assertNotIn("local_recovery", self.store.project_view(self.owner, self.project)["tasks"][0])
        self.backend.device_api(
            "/api/device/report", {**report, "state": "unknown", "result": {"error": "Unknown job"}}, token
        )
        coordinator.collect(task)
        result = self.store.cancel_task(self.owner, self.project, task)
        self.assertEqual((result["state"], result["cancel_requested"]), ("unknown", True))
        coordinator.collect(task)
        self.assertIn(job["id"], self.backend.device_api("/api/device/poll", {}, token)["cancellations"])
        self.backend.device_api("/api/device/report", {**report, "state": "cancelled"}, token)
        coordinator.collect(task)
        self.assertEqual(self.store.project_view(self.owner, self.project)["tasks"][0]["state"], "cancelled")

    def test_delegate_can_cancel_only_its_own_submission(self):
        token, _ = self.enroll()
        agent = self.advertise(token)
        task = self.create_task(agent, "submitting-delegate")
        peer = provision_account(self.store, "peer", invite=self.store.invite(self.owner, self.project)["invite"])["id"]
        with self.assertRaises(PermissionError):
            self.store.cancel_task(peer, self.project, task)
        with self.assertRaises(PermissionError):
            self.store.cancel_task(self.owner, self.project, task, "different-delegate")
        result = self.store.cancel_task(self.owner, self.project, task, "submitting-delegate")
        self.assertEqual((result["state"], result["cancel_requested"]), ("cancelled", False))
        Coordinator(self.store, self.backend).dispatch(task)
        self.assertEqual(self.backend.device_api("/api/device/poll", {}, token)["jobs"], [])

    def test_cancel_requested_after_dispatch_stops_assignment_before_claim(self):
        token, _ = self.enroll()
        task = self.create_task(self.advertise(token))
        coordinator = Coordinator(self.store, self.backend)
        coordinator.dispatch(task)
        self.assertTrue(self.store.cancel_task(self.owner, self.project, task)["cancel_requested"])
        # A device can poll before the coordinator's next tick.
        self.assertEqual(self.backend.device_api("/api/device/poll", {}, token)["jobs"], [])
        coordinator.collect(task)
        self.assertEqual(self.store.project_view(self.owner, self.project)["tasks"][0]["state"], "cancelled")

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

    def test_a_report_without_a_verifiable_model_is_accepted(self):
        """The DSH CLI cannot name the model that answered; that must not hang a turn."""
        token, _ = self.enroll()
        agent = self.advertise(token)
        self.submit(agent)
        job = self.claim(token)
        answer = self.backend.device_api(
            "/api/device/report",
            {
                "job_id": job["id"],
                "lease_id": job["lease_id"],
                "state": "needs_review",
                "result": {
                    "report": '{"reply": "hi"}',
                    "requested_model": "deepseek-flash",
                    "effective_model": None,
                    "model_verified": False,
                    "reasoning_effort": "high",
                },
            },
            token,
        )
        self.assertTrue(answer.get("ok"))
        with self.store.connect() as db:
            row = db.execute("SELECT state, result FROM pull_jobs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual(row["state"], "needs_review")
        self.assertIn("deepseek-flash", row["result"])

    def test_a_device_can_fetch_a_conversation_attachment(self):
        import base64

        from agent_harness.attachments import Attachments

        token, _ = self.enroll()
        file = Attachments(self.store).upload(self.owner, self.project, "shot.png", base64.b64encode(b"hi").decode())
        self.store.message(self.owner, self.project, "look", attachment_ids=[file["id"]])
        answer = self.backend.device_api("/api/device/task-inbound", {"ids": [file["id"]]}, token)
        self.assertEqual(answer["files"][0]["name"], "shot.png")
        self.assertEqual(base64.b64decode(answer["files"][0]["base64"]), b"hi")

    def test_another_account_cannot_fetch_that_attachment(self):
        import base64

        from agent_harness.attachments import Attachments

        file = Attachments(self.store).upload(self.owner, self.project, "shot.png", base64.b64encode(b"hi").decode())
        self.store.message(self.owner, self.project, "look", attachment_ids=[file["id"]])
        invite = self.store.invite(self.owner, self.project)["invite"]
        other = provision_account(self.store, "outsider", invite=invite)["id"]
        token, _ = self.enroll("outsider-worker", other)
        with self.assertRaises(PermissionError):
            self.backend.device_api("/api/device/task-inbound", {"ids": [file["id"]]}, token)

    def test_permission_request_does_not_create_a_standing_grant(self):
        token, _ = self.enroll()
        agent = self.advertise(token)
        coordinator = Coordinator(self.store, self.backend)
        source = self.create_task(agent)
        coordinator.dispatch(source)
        completed = self.claim(token)
        self.complete(
            token, completed, json.dumps({"reply": "Permission required", "request": {"reason": "Read report"}})
        )
        coordinator.collect(source)
        messages = self.store.project_view(self.owner, self.project)["messages"]
        request = next(m["permission_request"] for m in messages if "permission_request" in m)
        self.assertEqual(request["state"], "pending")
        invite = self.store.invite(self.owner, self.project)["invite"]
        peer = provision_account(self.store, "permission observer", invite=invite)["id"]
        view = self.store.project_view(peer, self.project)
        self.assertFalse(any("permission_request" in m or "Read report" in m["body"] for m in view["messages"]))
        with self.assertRaises(PermissionError):
            self.store.permission_decide(peer, self.project, request["id"], True)
        self.store.message(peer, self.project, "Unrelated later request")

        granted = self.store.permission_decide(self.owner, self.project, request["id"], True)
        self.assertEqual(self.store.permission_decide(self.owner, self.project, request["id"], True), granted)
        self.assertEqual(self.store.grants(agent["id"]), [])
        continuation = granted["task_id"]
        coordinator.dispatch(continuation)
        authorized = self.claim(token)
        context = json.loads(authorized["request"]["prompt"])
        self.assertEqual(context["grants"], ["full_access"])
        self.assertEqual(context["permission_request_id"], request["id"])
        self.assertEqual(context["approved_continuation"]["source_task_id"], source)
        self.assertEqual(context["task"]["instructions"], "Explain status")
        self.assertEqual(context["visible_messages"], [])
        self.assertEqual(self.claim(token), authorized)
        repeated = self.store.permission_decide(self.owner, self.project, request["id"], True)
        self.assertEqual(repeated["state"], "consumed")
        self.assertEqual(repeated["task_id"], continuation)
        self.complete(token, authorized, json.dumps({"reply": "Finished"}))
        coordinator.collect(continuation)
        unrelated = self.create_task(agent)
        coordinator.dispatch(unrelated)
        self.assertEqual(json.loads(self.claim(token)["request"]["prompt"])["grants"], [])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 3)

    def test_legacy_full_access_snapshot_cannot_be_claimed(self):
        token, _ = self.enroll()
        agent = self.advertise(token)
        self.store.set_grant(agent["id"], "full_access", True, actor=self.owner)
        context = Coordinator(self.store, self.backend).agent_context(agent, {"contract": "{}", "cutoff": 0})
        snapshot = json.loads(context)
        self.assertEqual(snapshot["grants"], [])
        snapshot["grants"] = ["full_access"]
        elevated = json.dumps(snapshot)
        with self.assertRaises(PermissionError):
            self.backend.submit("workspace", elevated, "discuss", "", "ok", "check", job_id="forged-elevation")
        job = "legacy-elevation"
        in_flight = "legacy-elevation-claimed"
        with self.store.connect() as db:
            self.backend._enqueue(db, job, agent, "task", {"prompt": elevated})
            self.backend._enqueue(db, in_flight, agent, "task", {"prompt": elevated})
            db.execute("UPDATE pull_jobs SET state='claimed' WHERE id=?", (in_flight,))
        unprivileged = self.submit(agent)

        poll = self.backend.device_api("/api/device/poll", {}, token)
        self.assertEqual([item["id"] for item in poll["jobs"]], [unprivileged["job"]])
        self.assertIn(in_flight, poll["cancellations"])
        self.assertEqual(self.backend.status(in_flight)["status"], "unknown")
        status = self.backend.status(job)
        self.assertEqual(status["status"], "cancelled")
        self.assertIn("one-time", status["error"])
        with self.store.connect() as db:
            saved = db.execute("SELECT request FROM pull_jobs WHERE id=?", (job,)).fetchone()[0]
        self.assertEqual(json.loads(saved)["prompt"], elevated)

    def test_private_permission_request_only_reaches_machine_owner_without_hidden_reason(self):
        invite = self.store.invite(self.owner, self.project)["invite"]
        peer = provision_account(self.store, "machine owner", invite=invite)["id"]
        token, _ = self.enroll("peer-worker", peer)
        agent = self.advertise(token, node="peer-worker")
        coordinator = Coordinator(self.store, self.backend)
        with self.store.connect() as db:
            project = self.store.require_member(db, self.project, self.owner)
            source = self.store._task(
                db,
                project,
                agent,
                {
                    "title": "Private task",
                    "instructions": "Handle the private task",
                    "acceptance": "Done",
                    "validation": "Review",
                    "private_turn": True,
                    "reply_to": self.owner,
                },
                self.owner,
            )["id"]
        coordinator.dispatch(source)
        job = self.claim(token)
        reason = "Private reason that belongs to the delegator"
        self.complete(token, job, json.dumps({"reply": "", "request": {"reason": reason}}))
        coordinator.collect(source)

        messages = self.store.project_view(peer, self.project)["messages"]
        approval_message = next(m for m in messages if "permission_request" in m)
        self.assertEqual(approval_message["recipient_id"], peer)
        self.assertNotIn(reason, approval_message["body"])
        request_id = approval_message["permission_request"]["id"]
        self.assertFalse(
            any("permission_request" in m for m in self.store.project_view(self.owner, self.project)["messages"])
        )
        with self.assertRaises(PermissionError):
            self.store.permission_decide(self.owner, self.project, request_id, True)
        self.assertEqual(self.store.permission_decide(peer, self.project, request_id, False)["state"], "denied")
        with self.assertRaises(ValueError):
            self.store.permission_decide(peer, self.project, request_id, True)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_a_corrupt_node_file_does_not_break_the_device_poll(self):
        """A crash can leave the node record full of zeros; polling must survive it."""
        token, pair = self.enroll()
        config = self.store.root / "nodes" / (pair["node"] + ".json")
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_bytes(b"\x00" * 705)
        health = {
            "schema_version": 1,
            "generated_at": time.time(),
            "overall": "healthy",
            "components": [{"id": "receiver", "state": "healthy", "code": "OK", "message": "ok"}],
        }
        result = self.backend.device_api("/api/device/poll", {"health": health}, token)
        self.assertIsInstance(result, dict)
        saved = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(saved["health"]["overall"], "healthy")

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
