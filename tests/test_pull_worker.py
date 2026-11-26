import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import os
from types import SimpleNamespace
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_harness import pull_worker
from agent_harness.common import read_json, write_json
from agent_harness.pull_worker import (
    Connector,
    deploy_runtime,
    validate_server,
    api,
    enroll,
)
from agent_harness.workspace_ownership import DIRTY_WORKTREE


class PullWorkerTests(unittest.TestCase):
    def test_deployed_runtime_contains_release_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deploy_runtime(root, Path(pull_worker.__file__).resolve().parent)
            expected = Path(__file__).resolve().parents[1].joinpath("VERSION").read_text(encoding="utf-8")
            self.assertEqual((root / "VERSION").read_text(encoding="utf-8"), expected)
            self.assertTrue((root / "release_info.py").is_file())

    def test_a_moved_receiver_uses_its_own_directory_not_a_stale_path(self):
        # Enrollment stores an absolute root. Moving user data leaves it pointing
        # at the old location; honouring it would recreate a second receiver there.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "receivers" / "device-1"
            root.mkdir(parents=True)
            (root / "worker.py").write_text("# receiver\n", encoding="utf-8")
            stale = Path(directory) / "old" / "receivers" / "device-1"
            write_json(root / "pull-config.json", {"server": "http://127.0.0.1:9", "root": str(stale)})
            connector = Connector(root / "pull-config.json")
            self.assertEqual(connector.root, root.resolve())
            self.assertEqual(read_json(root / "pull-config.json")["root"], str(root.resolve()))

    def test_a_configuration_kept_outside_a_receiver_root_keeps_its_recorded_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "receiver"
            root.mkdir()
            config = Path(directory) / "config.json"
            write_json(config, {"server": "http://127.0.0.1:9", "root": str(root)})
            self.assertEqual(Connector(config).root, root.resolve())

    def test_a_configuration_without_a_recorded_root_uses_its_own_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "pull-config.json"
            write_json(config, {"server": "http://127.0.0.1:9"})
            self.assertEqual(Connector(config).root, Path(directory).resolve())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        write_json(
            self.config,
            {
                "root": str(self.root),
                "server": "http://localhost",
                "device_token": "private-device-token-value",
            },
        )
        write_json(
            self.root / "projects" / "local" / "project.json",
            {
                "name": "local",
                "model": "owner-model",
                "effort": "high",
                "timeout": 120,
                "remote_path": str(self.root),
            },
        )
        self.calls = []
        self.reports = []
        self.jobs = []
        self.cancellations = []
        self.execution_decisions = []
        self.fail_report = False
        self.local_state = "needs_review"

        def executor(request):
            self.calls.append(request)
            if request["action"] == "fetch":
                return {"status": self.local_state, "report": "local-report"}
            if request["action"] == "execution_status":
                return {"status": "completed", "exit_code": 0}
            return {"status": "running"}

        def transport(config, operation, payload):
            if operation == "poll":
                return {
                    "jobs": self.jobs,
                    "projects": [],
                    "cancellations": self.cancellations,
                    "execution_decisions": self.execution_decisions,
                }
            if self.fail_report:
                raise OSError("offline")
            self.reports.append(payload)
            return {}

        self.connector = Connector(self.config, transport, executor)
        self.binding = {
            "project_id": "p1",
            "backend_project": "local",
            "model": "owner-model",
            "effort": "high",
            "timeout": 120,
            "stages": ["discuss"],
            "enabled": True,
        }
        self.connector.save_binding(self.binding)

    def job(self, name, kind="task", request=None):
        return {
            "id": name,
            "job_id": name,
            "lease_id": "fixed-lease",
            "project_id": "p1",
            "backend_project": "local",
            "agent_id": "agent1",
            "kind": kind,
            "request": request
            or {
                "prompt": json.dumps({"identity": {"id": "agent1"}, "project": {"id": "p1"}}),
                "stage": "discuss",
                "session_channel": "group",
            },
        }

    def starts(self, action="start"):
        return [r for r in self.calls if r["action"] == action]

    def test_busy_workspace_retries_same_lease_without_unknown_execution(self):
        self.jobs = [self.job("busy")]
        executor = self.connector.executor

        def temporarily_busy(request):
            if request["action"] == "start":
                self.calls.append(request)
                raise ValueError("Remote directory is busy in another project")
            return executor(request)

        self.connector.executor = temporarily_busy
        self.connector.cycle()
        waiting = read_json(self.connector.receipt_path("busy"))
        self.assertEqual((waiting["phase"], waiting["result"]["local_status"]), ("received", "workspace_busy"))
        self.assertEqual(self.reports[-1]["state"], "running")
        self.connector.executor = executor
        self.connector.cycle()
        self.assertEqual([call["job"] for call in self.starts()], [waiting["local_id"], waiting["local_id"]])
        self.connector.cycle()
        self.assertEqual(self.reports[-1]["state"], "needs_review")

    def test_uncommitted_changes_from_another_work_block_before_launch(self):
        self.jobs = [self.job("dirty")]
        executor = self.connector.executor

        def dirty_worktree(request):
            if request["action"] == "start":
                raise ValueError(DIRTY_WORKTREE)
            return executor(request)

        self.connector.executor = dirty_worktree
        self.connector.cycle()
        self.assertEqual(self.reports[-1]["state"], "failed")
        self.assertEqual(self.reports[-1]["result"]["error"], DIRTY_WORKTREE)
        self.assertTrue(read_json(self.connector.receipt_path("dirty"))["reported_terminal"])

    def test_old_reported_local_results_are_pruned_without_touching_active_work(self):
        old_id = "pull-" + "a" * 48
        active_id = "pull-" + "b" * 48
        old_receipt = self.connector.receipt_path("old")
        active_receipt = self.connector.receipt_path("active")
        write_json(
            old_receipt,
            {
                "job": {"kind": "task"},
                "local_id": old_id,
                "reported_terminal": True,
                "finished_at": 1,
            },
        )
        write_json(
            active_receipt,
            {
                "job": {"kind": "task"},
                "local_id": active_id,
                "reported_terminal": False,
                "finished_at": 1,
            },
        )
        (self.root / "jobs" / old_id).mkdir(parents=True)
        (self.root / "jobs" / active_id).mkdir(parents=True)
        removed = self.connector.prune_local_results(now=pull_worker.LOCAL_RESULT_RETENTION_SECONDS + 2)
        self.assertEqual(removed, 1)
        self.assertFalse(old_receipt.exists())
        self.assertFalse((self.root / "jobs" / old_id).exists())
        self.assertTrue(active_receipt.exists())
        self.assertTrue((self.root / "jobs" / active_id).exists())

    def test_poll_rejects_incompatible_hub_before_starting_work(self):
        self.jobs = [self.job("turn")]

        def incompatible(_config, operation, _payload):
            if operation == "poll":
                return {
                    "protocol": {"hub": 0, "minimum_client": 99},
                    "jobs": self.jobs,
                    "projects": [],
                    "cancellations": [],
                }
            return {}

        self.connector.transport = incompatible
        with self.assertRaisesRegex(RuntimeError, "升级"):
            self.connector.cycle()
        self.assertFalse(self.starts())

    def test_local_settings_and_duplicate_assignment(self):
        self.jobs = [self.job("turn")]
        for _ in range(4):
            self.connector.cycle()
        self.assertEqual(len(self.starts()), 1)
        configured = self.starts("configure")[0]
        self.assertEqual((configured["model"], configured["effort"]), ("owner-model", "high"))
        self.assertTrue(read_json(self.root / "projects" / "local" / "project.json")["local_control"])
        self.assertEqual(json.loads(self.starts()[0]["prompt"])["allowed_files"], "No edits")
        self.assertEqual(self.reports[-1]["state"], "needs_review")

    def test_project_tool_uses_task_mailbox_and_returns_hub_result(self):
        self.local_state = "running"
        self.jobs = [self.job("tool-turn")]
        calls = []

        def transport(_config, operation, payload):
            if operation == "poll":
                return {
                    "jobs": self.jobs,
                    "projects": [],
                    "cancellations": [],
                    "features": ["project_action_tools_v1"],
                }
            if operation == "project-tool":
                calls.append(payload)
                return {"ok": True, "id": "draft-1", "revision": 1, "status": "draft"}
            self.reports.append(payload)
            return {}

        self.connector.transport = transport
        self.connector.cycle()
        receipt = read_json(self.connector.receipt_path("tool-turn"))
        self.assertTrue(receipt["project_tools"])
        task_dir = self.root / "jobs" / receipt["local_id"]
        task_dir.mkdir(parents=True, exist_ok=True)
        request = task_dir / ("pt-request-" + "a" * 32 + ".json")
        write_json(request, {"action": {"kind": "create_draft", "title": "Smoke"}})

        self.connector.cycle()

        self.assertEqual(
            calls,
            [
                {
                    "job_id": "tool-turn",
                    "lease_id": "fixed-lease",
                    "action": {"kind": "create_draft", "title": "Smoke"},
                }
            ],
        )
        self.assertEqual(
            read_json(task_dir / ("pt-result-" + "a" * 32 + ".json")),
            {"ok": True, "id": "draft-1", "revision": 1, "status": "draft"},
        )

    def test_project_tool_keeps_request_for_retry_and_skips_cancelled_task(self):
        receipt = self.connector.accept(self.job("tool-retry"))
        receipt.update(phase="running", project_tools=True)
        task_dir = self.root / "jobs" / receipt["local_id"]
        task_dir.mkdir(parents=True)
        request = task_dir / ("pt-request-" + "b" * 32 + ".json")
        write_json(request, {"action": {"kind": "create_draft"}})
        attempts = []

        def offline(_config, operation, payload):
            if operation == "project-tool":
                attempts.append(payload)
                raise OSError("offline")
            return {}

        self.connector.transport = offline
        self.connector._service_project_tools(receipt, False)
        self.assertEqual(len(attempts), 1)
        self.assertFalse((task_dir / ("pt-result-" + "b" * 32 + ".json")).exists())
        self.connector._service_project_tools(receipt, True)
        self.assertEqual(len(attempts), 1)

    def test_project_tool_rejects_malformed_mailbox_request_with_tool_result(self):
        receipt = self.connector.accept(self.job("tool-malformed"))
        receipt.update(phase="running", project_tools=True)
        task_dir = self.root / "jobs" / receipt["local_id"]
        task_dir.mkdir(parents=True)
        token = "c" * 32
        write_json(task_dir / ("pt-request-" + token + ".json"), {"action": {}, "extra": True})

        self.connector._service_project_tools(receipt, False)

        self.assertEqual(
            read_json(task_dir / ("pt-result-" + token + ".json")),
            {
                "ok": False,
                "error_code": "invalid_request",
                "message": "任务工具请求只能包含 action 对象",
            },
        )

    def test_implementation_handoff_supplies_the_canonical_allowed_files(self):
        self.connector.save_binding(dict(self.binding, stages=["implement"]))
        request = {
            "prompt": json.dumps({"identity": {"id": "agent1"}, "project": {"id": "p1"}}),
            "stage": "implement",
        }
        scope = "README.md\nsrc/"
        for name, fields, expected in (("specified", {"scope": scope}, scope), ("missing", {}, "")):
            with self.subTest(scope=name):
                job = self.job(name, request={**request, **fields})
                receipt = self.connector.advance(self.connector.accept(job))
                self.assertEqual(receipt["phase"], "running")
                contract = json.loads(self.starts()[-1]["prompt"])
                self.assertEqual(contract["allowed_files"], expected)

    def test_remote_configuration_and_identity_are_rejected(self):
        for index, request in enumerate(
            [
                {"model": "remote-model", "argv": ["evil"]},
                {"stage": "implement", "prompt": "{}"},
                {
                    "stage": "discuss",
                    "prompt": json.dumps({"identity": {"id": "wrong"}, "project": {"id": "p1"}}),
                },
            ]
        ):
            self.jobs = [self.job("bad" + str(index), request=request)]
            self.connector.cycle()
            self.assertEqual(self.reports[-1]["state"], "failed")
        self.assertFalse(self.starts())

    def test_optional_display_name_defaults_to_local_project(self):
        self.connector.save_binding(dict(self.binding, name=""))
        advertisement = self.connector.advertisements()[0]
        self.assertEqual(advertisement["name"], "local")
        self.assertNotIn("remote_path", advertisement)

    @unittest.skipUnless(os.name == "nt", "Windows credential permissions")
    def test_failed_credential_acl_removes_new_token_file(self):
        root = self.root / "enrollment"
        with (
            patch("agent_harness.pull_worker.api", return_value={"node": "local"}),
            patch(
                "agent_harness.pull_worker.subprocess.check_output",
                return_value='"user","S-1-5-21-123"',
            ),
            patch(
                "agent_harness.pull_worker.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ),
        ):
            with self.assertRaises(RuntimeError):
                enroll("https://example.com", "one-time-code", root)
        self.assertFalse((root / "pull-config.json").exists())

    def test_execution_requires_local_approval_and_template_is_frozen(self):
        template = self.root / "projects" / "local" / "execution_templates.json"
        write_json(template, {"check": {"argv": ["fixed-program", "arg"]}})
        self.jobs = [self.job("exec", "execution", {"template": "check"})]
        self.connector.cycle()
        self.connector.cycle()
        self.assertFalse(self.starts("execution_start"))
        self.assertEqual(self.reports[-1]["result"]["local_status"], "waiting_local_approval")
        self.connector.approve("exec", True)
        self.connector.cycle()
        self.connector.cycle()
        self.assertEqual(len(self.starts("execution_start")), 1)
        self.jobs = [self.job("changed", "execution", {"template": "check"})]
        self.connector.cycle()
        self.connector.approve("changed", True)
        write_json(template, {"check": {"argv": ["other-program"]}})
        self.connector.cycle()
        self.assertEqual(self.reports[-1]["state"], "failed")
        self.assertEqual(len(self.starts("execution_start")), 1)

    def test_delegated_execution_uses_exact_snapshot_and_is_not_replayed(self):
        template = self.root / "projects" / "local" / "execution_templates.json"
        write_json(template, {"check": {"argv": ["fixed-program", "arg"]}})
        self.jobs = [self.job("exec", "execution", {"template": "check"})]
        self.connector.cycle()
        pending = self.reports[-1]["result"]
        self.assertEqual(pending["local_status"], "waiting_local_approval")
        self.assertEqual(set(pending), {"status", "local_status", "execution_approval_digest"})
        digest = pending["execution_approval_digest"]
        self.assertEqual(len(digest), 64)
        approved = {"job_id": "exec", "digest": digest, "approved": True}
        self.execution_decisions = [{**approved, "digest": "0" * 64}]
        self.connector.cycle()
        self.assertFalse(self.starts("execution_start"))

        self.execution_decisions = [approved]
        self.connector.cycle()
        self.connector.cycle()
        self.connector.cycle()
        self.assertEqual(len(self.starts("execution_start")), 1)
        self.assertEqual(self.starts("execution_start")[0]["expected_execution_digest"], digest)
        self.assertEqual(self.reports[-1]["state"], "completed")

    def test_delegated_execution_rejects_changed_templates_and_cancellation_wins(self):
        template = self.root / "projects" / "local" / "execution_templates.json"
        for case in ("mutated", "cancelled", "denied"):
            with self.subTest(case=case):
                write_json(template, {"check": {"argv": ["fixed-program", "arg"]}})
                self.jobs = [self.job(case, "execution", {"template": "check"})]
                self.connector.cycle()
                digest = self.reports[-1]["result"]["execution_approval_digest"]
                self.execution_decisions = [{"job_id": case, "digest": digest, "approved": case != "denied"}]
                if case == "mutated":
                    write_json(template, {"check": {"argv": ["different-program"]}})
                if case == "cancelled":
                    self.cancellations = [case]
                self.connector.cycle()
                self.assertEqual(self.reports[-1]["state"], "failed" if case == "mutated" else "cancelled")
                self.assertFalse(self.starts("execution_start"))

    def test_prelaunch_snapshot_refusal_is_terminal_without_reconciliation(self):
        write_json(
            self.root / "projects" / "local" / "execution_templates.json",
            {"check": {"argv": ["fixed-program"]}},
        )
        self.jobs = [self.job("exec", "execution", {"template": "check"})]
        self.connector.cycle()
        digest = self.reports[-1]["result"]["execution_approval_digest"]
        self.execution_decisions = [{"job_id": "exec", "digest": digest, "approved": True}]
        self.connector.executor = lambda request: {"status": "failed", "error": "Snapshot changed under lock"}
        self.connector.cycle()
        stored = read_json(self.connector.receipt_path("exec"))
        self.assertEqual(stored["phase"], "terminal")
        self.assertEqual(self.reports[-1]["state"], "failed")
        self.assertEqual(self.reports[-1]["result"]["error"], "Snapshot changed under lock")

    def test_lost_report_retries_without_reexecution(self):
        self.jobs = [self.job("turn")]
        self.connector.cycle()
        self.fail_report = True
        with self.assertRaises(OSError):
            self.connector.cycle()
        self.fail_report = False
        self.connector.cycle()
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.reports[-1]["state"], "needs_review")

    def test_cancel_while_attachment_approval_pending_does_not_post_reply(self):
        job = self.job("attachment-wait")
        receipt = self.connector.accept(job)
        receipt.update(
            phase="waiting_attachment_approval",
            result={
                "status": "needs_review",
                "report": json.dumps({"reply": "unsent", "attachments": [{"path": "file.txt"}]}),
            },
            attachment_approval={"id": "approval", "digest": "digest"},
        )
        write_json(self.connector.receipt_path(job["id"]), receipt)
        self.jobs = [job]
        self.cancellations = [job["id"]]
        self.connector.cycle()
        self.assertEqual(self.reports[-1]["state"], "cancelled")
        self.assertNotIn("report", self.reports[-1]["result"])

    def test_crash_launch_receipt_reconciles_instead_of_resubmitting(self):
        job = self.job("ambiguous")
        receipt = self.connector.accept(job)
        receipt["phase"] = "launching"
        write_json(self.connector.receipt_path("ambiguous"), receipt)
        self.jobs = [job]
        self.connector.cycle()
        self.assertFalse(self.starts())
        self.assertEqual(self.reports[-1]["state"], "needs_review")

    def test_disable_running_binding_does_not_cancel_existing_turn(self):
        self.local_state = "running"
        self.jobs = [self.job("turn")]
        self.connector.cycle()
        self.connector.save_binding(dict(self.binding, enabled=False))
        self.connector.cycle()
        self.assertFalse(self.starts("cancel"))
        self.assertEqual(self.reports[-1]["state"], "running")
        self.local_state = "cancelled"
        self.connector.cycle()
        self.assertEqual(self.reports[-1]["state"], "cancelled")

    def test_runtime_update_refuses_unknown_or_active_supervisor(self):
        state = self.root / "jobs" / "unknown" / "state.json"
        write_json(state, {"status": "unknown"})
        with self.assertRaises(ValueError):
            deploy_runtime(self.root)
        state.unlink()
        write_json(self.root / "projects" / "local" / "active.json", {"job": "missing-state"})
        with self.assertRaises(ValueError):
            deploy_runtime(self.root)

    def test_unknown_local_reconciliation_never_restarts(self):
        receipt = self.connector.accept(self.job("unknown"))
        receipt.update(phase="terminal", result={"status": "unknown"}, reported_terminal=True)
        write_json(self.connector.receipt_path("unknown"), receipt)
        result = self.connector.resolve("unknown")
        self.assertEqual(result["status"], "needs_review")
        self.assertFalse(self.starts())
        self.connector.cycle()
        self.assertEqual(self.reports[-1]["state"], "needs_review")

    def test_transient_status_failure_never_requests_cancellation(self):
        receipt = self.connector.accept(self.job("transient"))
        receipt["phase"] = "running"
        write_json(self.connector.receipt_path("transient"), receipt)
        calls = []

        def executor(request):
            calls.append(request["action"])
            if request["action"] == "fetch":
                raise OSError("temporary observer failure")
            return {"status": "running"}

        self.connector.executor = executor
        result = self.connector.advance(receipt)
        self.assertEqual(result["phase"], "running")
        self.assertEqual(result["result"]["status"], "unknown")
        self.assertEqual(calls, ["fetch"])
        self.connector.cycle()
        self.assertEqual(self.reports[-1]["state"], "unknown")
        self.assertFalse(self.starts())

    def test_unknown_no_job_requires_explicit_owner_confirmation(self):
        receipt = self.connector.accept(self.job("missing"))
        receipt.update(phase="terminal", result={"status": "unknown"}, reported_terminal=True)
        write_json(self.connector.receipt_path("missing"), receipt)

        def missing(_):
            raise ValueError("Unknown job")

        self.connector.executor = missing
        with self.assertRaises(ValueError):
            self.connector.resolve("missing")
        self.assertEqual(self.connector.resolve("missing", True)["status"], "cancelled")
        self.assertFalse(self.starts())

    def test_task_confirmation_preserves_logs_and_blocks_an_active_local_record(self):
        receipt = self.connector.accept(self.job("missing"))
        receipt.update(phase="running", result={"status": "unknown"})
        write_json(self.connector.receipt_path("missing"), receipt)
        log = self.root / "jobs" / receipt["local_id"] / "stderr.log"
        log.parent.mkdir(parents=True)
        log.write_text("preserve this evidence", encoding="utf-8")
        active = self.root / "projects" / "local" / "active.json"
        write_json(active, {"job": receipt["local_id"]})

        def missing(_):
            raise ValueError("Unknown job")

        self.connector.executor = missing
        with self.assertRaisesRegex(ValueError, "active record"):
            self.connector.resolve("missing", True, kind="task")
        with self.assertRaisesRegex(ValueError, "expected job type"):
            self.connector.resolve("missing", True, kind="execution")
        write_json(active, None)
        result = self.connector.resolve("missing", True, kind="task")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.connector.resolve("missing", True, kind="task"), result)
        self.assertEqual(log.read_text(encoding="utf-8"), "preserve this evidence")
        self.connector.cycle(wait_seconds=0)
        self.assertEqual(self.reports[-1]["state"], "cancelled")
        self.assertFalse(self.starts())

    def test_confirmation_cannot_turn_an_observer_error_into_no_job(self):
        receipt = self.connector.accept(self.job("unreachable"))
        receipt.update(phase="running", result={"status": "unknown"})
        write_json(self.connector.receipt_path("unreachable"), receipt)

        def unreachable(_):
            raise OSError("temporary connection failure")

        self.connector.executor = unreachable
        with self.assertRaisesRegex(ValueError, "verify whether this task exists"):
            self.connector.resolve("unreachable", True, kind="task")
        self.assertEqual(read_json(self.connector.receipt_path("unreachable"))["phase"], "running")

    def test_elevated_assignment_requires_a_one_time_approval_marker(self):
        approval = "permission-" + "a" * 32
        for job_id, continuation in (
            ("legacy", None),
            ("mismatch", {"id": "permission-" + "b" * 32}),
            ("approved", {"id": approval, "source_task_id": "task-original"}),
        ):
            with self.subTest(job=job_id):
                context = {"identity": {"id": "agent1"}, "project": {"id": "p1"}, "grants": ["full_access"]}
                if continuation is not None:
                    context.update(permission_request_id=approval, approved_continuation=continuation)
                job = self.job(job_id, request={"prompt": json.dumps(context), "stage": "discuss"})
                receipt = self.connector.advance(self.connector.accept(job))
                if job_id == "approved":
                    self.assertEqual(receipt["phase"], "running")
                    self.assertEqual(len(self.starts()), 1)
                else:
                    self.assertEqual(receipt["phase"], "terminal")
                    self.assertEqual(receipt["result"]["status"], "failed")
                    self.assertFalse(self.starts())

    def test_non_loopback_broker_requires_https(self):
        for url in (
            "http://100.98.57.38:8765",
            "http://example.com",
            "https://user:pass@example.com",
            "https://example.com/path",
        ):
            with self.assertRaises(ValueError):
                validate_server(url)
        for url in (
            "https://example.com",
            "http://127.0.0.1:18876",
            "http://localhost:8765",
            "http://[::1]:8765",
        ):
            validate_server(url)

    def test_broker_redirect_is_rejected_without_following_it(self):
        calls = []

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                calls.append(self.path)
                self.send_response(307)
                self.send_header("Location", "/credential-receiver")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # urllib normally surfaces the rejected 307 as HTTPError. Windows may
        # instead report a local connection abort while the server closes the
        # rejected response; in both cases the security property is that the
        # credential-bearing POST is never replayed to the Location target.
        with self.assertRaises((urllib.error.HTTPError, ConnectionError)):
            api(
                {
                    "server": f"http://127.0.0.1:{server.server_port}",
                    "device_token": "test-token",
                },
                "poll",
                {},
            )
        self.assertEqual(calls, ["/api/device/poll"])

    def test_real_worker_registration_configuration(self):
        deploy_runtime(self.root)
        real = Connector(self.config)
        real.configure(self.binding)
        project = real.rpc({"action": "project", "project": "local"})["project"]
        self.assertEqual(project["model"], "owner-model")
        self.assertEqual(project["effort"], "high")
        self.assertTrue(project["local_control"])


class ExecutorHealthTests(unittest.TestCase):
    """The heartbeat must describe the CLI the model table actually uses."""

    def connector(self, root):
        connector = Connector.__new__(Connector)
        connector.root = root
        return connector

    def executor_component(self, health):
        return next(item for item in health["components"] if item["id"] == "executor")

    def test_a_dsh_only_machine_is_not_reported_as_codex(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"models": [{"name": "deepseek-flash", "executor": "dsh"}]})
            with patch("agent_harness.executors.status_of", return_value={"found": True}) as probe:
                component = self.executor_component(self.connector(root)._health())
            self.assertEqual(component["state"], "healthy")
            self.assertEqual(component["message"], "已找到 DeepSeek Harness")
            self.assertEqual(probe.call_args[0][1], "dsh")

    def test_an_auto_detected_binary_counts_as_configured(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"models": [{"name": "gpt-5", "executor": "codex"}]})
            with patch("agent_harness.executors.status_of", return_value={"found": True, "configured": False}):
                component = self.executor_component(self.connector(root)._health())
            self.assertEqual(component["state"], "healthy")
            self.assertEqual(component["message"], "已找到 Codex")

    def test_a_missing_cli_is_named_in_the_message(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"models": [{"name": "gpt-5", "executor": "codex"}]})
            with patch("agent_harness.executors.status_of", return_value={"found": False}):
                component = self.executor_component(self.connector(root)._health())
            self.assertEqual(component["state"], "degraded")
            self.assertEqual(component["code"], "CLI_NOT_FOUND")
            self.assertIn("未找到 Codex CLI", component["message"])

    def test_the_probe_is_cached_between_heartbeats(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"models": [{"name": "gpt-5", "executor": "codex"}]})
            connector = self.connector(root)
            with patch("agent_harness.executors.status_of", return_value={"found": True}) as probe:
                connector._health()
                connector._health()
            self.assertEqual(probe.call_count, 1)


if __name__ == "__main__":
    unittest.main()
