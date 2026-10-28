import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import worker
from agent_harness.common import (
    events,
    identifier,
    outcome,
    read_json,
    rotate_log,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "agent_harness"


class CommonTests(unittest.TestCase):
    def test_outcome_terminal_states(self):
        completed = [{"type": "turn.completed"}]
        self.assertEqual(outcome(0, completed), "needs_review")
        self.assertEqual(outcome(1, completed), "failed")
        self.assertEqual(outcome(0, [{"type": "turn.failed"}]), "failed")
        self.assertEqual(outcome(0, [{"type": "error"}]), "failed")
        recovered = [
            {"type": "error", "message": "WebSocket timed out; switching to HTTPS"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            },
            {"type": "turn.completed"},
        ]
        self.assertEqual(outcome(0, recovered), "needs_review")
        self.assertEqual(outcome(1, recovered), "failed")
        self.assertEqual(outcome(0, recovered + [{"type": "turn.failed"}]), "failed")
        self.assertEqual(outcome(0, completed, cancelled=True), "cancelled")
        self.assertEqual(outcome(0, completed, timed_out=True), "timed_out")
        self.assertEqual(outcome(0, completed, cancelled=True, timed_out=True), "cancelled")

    def test_json_round_trip_preserves_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            value = {"message": "实验完成 ✓", "items": ["显存", "résumé"]}
            write_json(path, value)
            self.assertEqual(read_json(path), value)
            self.assertIn("实验完成", path.read_text(encoding="utf-8"))

    def test_json_atomic_temp_name_does_not_extend_long_target_name(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            parent = base / ("d" * max(8, 170 - len(str(base))))
            parent.mkdir()
            path = parent / (("r" * 64) + ".json")
            self.assertGreater(len(str(path)), 230)
            write_json(path, {"status": "received"})
            self.assertEqual(read_json(path), {"status": "received"})
            self.assertEqual(list(parent.glob(".ah-*.tmp")), [])

    def test_events_ignores_malformed_and_non_event_lines(self):
        text = '{"type":"thread.started"}\nnot json\n{"x": 1}\n{"type":"item.completed"}\n'
        self.assertEqual(events(text), [{"type": "thread.started"}, {"type": "item.completed"}])

    def test_log_rotation_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "service.log"
            path.write_bytes(b"a" * 2048)
            self.assertTrue(rotate_log(path, max_bytes=1024, backups=2))
            self.assertFalse(path.exists())
            self.assertEqual((root / "service.log.1").stat().st_size, 2048)
            path.write_bytes(b"b" * 2048)
            self.assertTrue(rotate_log(path, max_bytes=1024, backups=2))
            self.assertEqual((root / "service.log.2").stat().st_size, 2048)

    def test_identifier_rejects_traversal_and_invalid_names(self):
        for value in ("../escape", "a/b", "", "-bad", "a.b", "a" * 65):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    identifier(value)
        self.assertEqual(identifier("project_1-okay"), "project_1-okay")


class WorkerTests(unittest.TestCase):
    def _project(self, name, path):
        return {
            "name": name,
            "model": "gpt-5.6-sol",
            "effort": "medium",
            "remote_path": str(path),
            "timeout": 30,
        }

    def test_build_command_pins_model_and_effort(self):
        project = {
            "model": "gpt-5.6-sol",
            "effort": "high",
            "remote_path": r"C:\work\project",
        }
        command = worker.build_command("codex.exe", project, None)
        self.assertIn("-m", command)
        self.assertEqual(command[command.index("-m") + 1], "gpt-5.6-sol")
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertEqual(command[-1], "-")

    def test_build_command_resumes_valid_session(self):
        project = {
            "model": "gpt-5.6-sol",
            "effort": "medium",
            "remote_path": r"C:\work\project",
        }
        thread = "01234567-89ab-cdef-0123-456789abcdef"
        command = worker.build_command("codex.exe", project, thread)
        self.assertEqual(command[-3:], ["resume", thread, "-"])

    def test_build_command_rejects_malformed_session(self):
        project = {
            "model": "gpt-5.6-sol",
            "effort": "medium",
            "remote_path": r"C:\work\project",
        }
        with self.assertRaises(ValueError):
            worker.build_command("codex.exe", project, "../../session")

    def test_launch_failure_writes_terminal_failed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            old_root = worker.ROOT
            worker.ROOT = Path(directory)
            try:
                remote = Path(directory) / "remote"
                remote.mkdir()
                project = self._project("demo", remote)
                worker.rpc(
                    {
                        "action": "register",
                        "project": project,
                        "memory": {"revision": 0, "notes": ""},
                    }
                )
                with mock.patch.object(
                    worker,
                    "launch_supervisor",
                    side_effect=RuntimeError("launch denied"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "launch denied"):
                        worker.rpc(
                            {
                                "action": "start",
                                "project": "demo",
                                "job": "job1",
                                "prompt": json.dumps({"stage": "discuss"}),
                            }
                        )
                state = worker.read_json(worker.job_dir("job1") / "state.json")
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["error"], "launch denied")
            finally:
                worker.ROOT = old_root

    def test_start_rejects_overlapping_canonical_remote_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            old_root = worker.ROOT
            worker.ROOT = Path(directory)
            try:
                remote = Path(directory) / "remote"
                child = remote / "child"
                child.mkdir(parents=True)
                p1 = self._project("one", remote)
                p2 = self._project("two", child)
                worker.rpc({"action": "register", "project": p1, "memory": {"revision": 0}})
                worker.rpc({"action": "register", "project": p2, "memory": {"revision": 0}})
                worker.write_json(
                    worker.project_dir("one") / "active.json",
                    {"job": "existing", "pid": 1, "stamp": "stamp"},
                )

                def preserve_other_active(active):
                    return active if active and active.get("job") == "existing" else None

                with mock.patch.object(worker, "reconcile", side_effect=preserve_other_active):
                    with self.assertRaisesRegex(ValueError, "busy in another project"):
                        worker.rpc(
                            {
                                "action": "start",
                                "project": "two",
                                "job": "job2",
                                "prompt": json.dumps({"stage": "implement"}),
                            }
                        )
            finally:
                worker.ROOT = old_root

    def test_read_only_turns_can_share_the_same_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            old_root = worker.ROOT
            worker.ROOT = Path(directory)
            try:
                remote = Path(directory) / "remote"
                remote.mkdir()
                worker.rpc({"action": "register", "project": self._project("one", remote), "memory": {"revision": 0}})
                worker.rpc({"action": "register", "project": self._project("two", remote), "memory": {"revision": 0}})
                worker.write_json(
                    worker.project_dir("one") / "active.json",
                    {"job": "existing", "pid": 1, "stamp": "stamp", "stage": "discuss"},
                )

                def preserve_other_active(active):
                    return active if active and active.get("job") == "existing" else None

                with (
                    mock.patch.object(worker, "reconcile", side_effect=preserve_other_active),
                    mock.patch.object(worker, "launch_supervisor", return_value=123),
                    mock.patch.object(worker, "process_stamp", return_value="new-stamp"),
                ):
                    result = worker.rpc(
                        {
                            "action": "start",
                            "project": "two",
                            "job": "job2",
                            "prompt": json.dumps({"stage": "review"}),
                        }
                    )
                self.assertEqual(result["status"], "starting")
            finally:
                worker.ROOT = old_root

    def test_implement_rejects_directory_that_is_not_linked_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            old_root = worker.ROOT
            worker.ROOT = Path(directory)
            try:
                remote = Path(directory) / "plain-project"
                remote.mkdir()
                project = self._project("plain", remote)
                worker.rpc(
                    {
                        "action": "register",
                        "project": project,
                        "memory": {"revision": 0},
                    }
                )
                with self.assertRaisesRegex(ValueError, "dedicated linked Git worktree"):
                    worker.rpc(
                        {
                            "action": "start",
                            "project": "plain",
                            "job": "impl1",
                            "prompt": json.dumps({"stage": "implement"}),
                        }
                    )
                self.assertFalse((worker.job_dir("impl1")).exists())
            finally:
                worker.ROOT = old_root

    def test_require_worktree_accepts_real_linked_git_worktree(self):
        import shutil

        git = shutil.which("git")
        if not git:
            self.skipTest("Git unavailable")
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            peer = Path(directory) / "peer"
            repo.mkdir()
            subprocess.run(
                [git, "-C", str(repo), "init", "--quiet"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    git,
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=HarnessTest",
                    "-c",
                    "user.email=harness@example.invalid",
                    "commit",
                    "--quiet",
                    "--allow-empty",
                    "-m",
                    "test",
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    git,
                    "-C",
                    str(repo),
                    "worktree",
                    "add",
                    "--quiet",
                    "--detach",
                    str(peer),
                    "HEAD",
                ],
                check=True,
                capture_output=True,
            )
            result = worker.require_worktree(self._project("demo", peer))
            self.assertEqual(Path(result["worktree"]), peer.resolve())
            self.assertNotEqual(result["git_dir"], result["common_dir"])

    def test_rpc_entrypoint_accepts_utf8_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("common.py", "worker.py"):
                (root / name).write_bytes((SOURCE / name).read_bytes())
            remote = root / "remote"
            remote.mkdir()
            payload = {
                "action": "register",
                "project": self._project("utf8", remote),
                "memory": {"revision": 0, "message": "实验室 ✓"},
            }
            import os

            env = dict(os.environ, PYTHONIOENCODING="ascii", PYTHONUTF8="0")
            result = subprocess.run(
                [sys.executable, str(root / "worker.py"), "rpc"],
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                capture_output=True,
                env=env,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["ok"])
            self.assertEqual(
                read_json(root / "projects" / "utf8" / "memory.json")["message"],
                "实验室 ✓",
            )

    def test_cancel_request_is_recorded_for_running_job(self):
        with tempfile.TemporaryDirectory() as directory:
            old_root = worker.ROOT
            worker.ROOT = Path(directory)
            try:
                project_dir = worker.project_dir("demo")
                project_dir.mkdir(parents=True)
                worker.write_json(project_dir / "session.json", {"thread_id": None})
                job_dir = worker.job_dir("job1")
                job_dir.mkdir(parents=True)
                worker.write_json(job_dir / "state.json", {"project": "demo", "status": "running"})
                worker.write_json(
                    project_dir / "active.json",
                    {"job": "job1", "pid": 1, "stamp": "stamp"},
                )
                with mock.patch.object(worker, "reconcile", side_effect=lambda active: active):
                    result = worker.rpc({"action": "cancel", "job": "job1"})
                cancel = worker.read_json(job_dir / "cancel.json")
                self.assertIn("requested_at", cancel)
                self.assertEqual(result["cancel_requested_at"], cancel["requested_at"])
            finally:
                worker.ROOT = old_root

    def test_cancel_before_terminal_commit_wins_over_completed_cli(self):
        self._run_mocked_job(cancel=True)

    def test_reader_failure_after_completion_event_is_not_success(self):
        self._run_mocked_job(reader_failure=True)

    def test_recoverable_transport_error_keeps_completed_report(self):
        self._run_mocked_job(recoverable_error=True)

    def _run_mocked_job(self, cancel=False, reader_failure=False, recoverable_error=False):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(worker, "ROOT", Path(directory)),
        ):
            remote = Path(directory) / "remote"
            remote.mkdir()
            project = self._project("demo", remote)
            job = worker.job_dir("job1")
            job.mkdir(parents=True)
            write_json(
                job / "request.json",
                {"project": project, "prompt": "test", "memory": {}, "thread_id": None},
            )
            write_json(job / "state.json", {"project": "demo", "status": "starting"})
            if cancel:
                write_json(job / "cancel.json", {"requested_at": "2026-10-01T00:00:00+00:00"})
            proc = mock.Mock(pid=123)
            proc.poll.return_value = 0  # CLI already exited before supervisor polling.
            proc.wait.return_value = 0
            proc.stdout = [b"report\n", b"complete\n", b"extra\n"]
            event_results = [
                [
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "report"},
                    }
                ],
                [{"type": "turn.completed"}],
                RuntimeError("reader-write-failure") if reader_failure else [],
            ]
            if recoverable_error:
                event_results[0].insert(
                    0,
                    {
                        "type": "error",
                        "message": "WebSocket timeout; switching to HTTPS",
                    },
                )
            with (
                mock.patch.object(worker, "cli_path", return_value="codex.exe"),
                mock.patch.object(worker, "codex_login_status", return_value={"logged_in": True}),
                mock.patch.object(worker.subprocess, "Popen", return_value=proc),
                mock.patch.object(worker, "events", side_effect=event_results),
                mock.patch.object(worker, "effective_model", return_value="gpt-5.6-sol"),
            ):
                worker.run("job1")
            state = read_json(job / "state.json")
            self.assertEqual(
                state["status"],
                "cancelled" if cancel else "needs_review" if recoverable_error else "failed",
            )
            if cancel:
                self.assertIn("cancel_requested_at", state)
            elif recoverable_error:
                self.assertEqual((job / "report.txt").read_text(encoding="utf-8"), "report")
            else:
                self.assertIn("reader-write-failure", state["error"])


if __name__ == "__main__":
    unittest.main()
