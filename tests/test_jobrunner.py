import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from agent_harness import jobrunner
from agent_harness.common import read_json, write_json


class JobRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project_path = self.root / "project"
        self.project_path.mkdir()
        self.project = {
            "name": "demo",
            "model": "gpt-5.6-sol",
            "effort": "medium",
            "remote_path": str(self.project_path),
            "timeout": 30,
        }
        (self.root / "projects" / "demo").mkdir(parents=True)
        write_json(self.root / "projects" / "demo" / "project.json", self.project)

    def tearDown(self):
        self.tmp.cleanup()

    def _template(self, name="sleep", seconds="0"):
        exe = Path(sys.executable).resolve()
        return {name: {"argv": [str(exe), "-c", f"import time; time.sleep({seconds})"]}}

    def test_templates_require_existing_absolute_executable(self):
        with self.assertRaises(ValueError):
            jobrunner.rpc(
                self.root,
                {
                    "action": "execution_templates",
                    "project": "demo",
                    "templates": {"bad": {"argv": ["relative.exe"]}},
                },
            )

    def test_start_uses_only_approved_template_and_duplicate_rejected(self):
        jobrunner.rpc(
            self.root,
            {
                "action": "execution_templates",
                "project": "demo",
                "templates": self._template(),
            },
        )
        with self.assertRaises(ValueError):
            jobrunner.rpc(
                self.root,
                {
                    "action": "execution_start",
                    "project": "demo",
                    "execution": "x",
                    "template": "missing",
                    "argv": ["evil.exe"],
                },
            )
        with (
            mock.patch.object(jobrunner, "_launch", return_value=123),
            mock.patch.object(jobrunner, "_stamp", return_value="stamp"),
        ):
            jobrunner.rpc(
                self.root,
                {
                    "action": "execution_start",
                    "project": "demo",
                    "execution": "x",
                    "template": "sleep",
                },
            )
        with self.assertRaises(ValueError):
            jobrunner.rpc(
                self.root,
                {
                    "action": "execution_start",
                    "project": "demo",
                    "execution": "x",
                    "template": "sleep",
                },
            )

    def test_dead_supervisor_is_terminal_not_permanently_starting(self):
        jobrunner.rpc(
            self.root,
            {
                "action": "execution_templates",
                "project": "demo",
                "templates": self._template(),
            },
        )
        with (
            mock.patch.object(jobrunner, "_launch", return_value=123),
            mock.patch.object(jobrunner, "_stamp", return_value=None),
        ):
            with self.assertRaises(RuntimeError):
                jobrunner.rpc(
                    self.root,
                    {
                        "action": "execution_start",
                        "project": "demo",
                        "execution": "dead",
                        "template": "sleep",
                    },
                )
            state = jobrunner.rpc(self.root, {"action": "execution_status", "execution": "dead"})
            self.assertEqual(state["status"], "failed")

    def test_async_execution_completes_and_status_has_output(self):
        jobrunner.rpc(
            self.root,
            {
                "action": "execution_templates",
                "project": "demo",
                "templates": {
                    "write": {
                        "argv": [
                            str(Path(sys.executable).resolve()),
                            "-c",
                            "print('done')",
                        ]
                    }
                },
            },
        )
        result = jobrunner.rpc(
            self.root,
            {
                "action": "execution_start",
                "project": "demo",
                "execution": "async1",
                "template": "write",
            },
        )
        self.assertEqual(result["status"], "starting")
        for _ in range(100):
            state = jobrunner.rpc(self.root, {"action": "execution_status", "execution": "async1"})
            if state["status"] in jobrunner.TERMINAL:
                break
            time.sleep(0.02)
        self.assertEqual(state["status"], "completed")
        self.assertIn("done", state["stdout_tail"])

    def test_cancel_and_timeout(self):
        jobrunner.rpc(
            self.root,
            {
                "action": "execution_templates",
                "project": "demo",
                "templates": self._template(seconds="60"),
            },
        )
        jobrunner.rpc(
            self.root,
            {
                "action": "execution_start",
                "project": "demo",
                "execution": "cancel1",
                "template": "sleep",
            },
        )
        time.sleep(0.1)
        state = jobrunner.rpc(self.root, {"action": "execution_cancel", "execution": "cancel1"})
        for _ in range(100):
            state = jobrunner.rpc(self.root, {"action": "execution_status", "execution": "cancel1"})
            if state["status"] in jobrunner.TERMINAL:
                break
            time.sleep(0.02)
        self.assertEqual(state["status"], "cancelled")

    def test_timeout_stops_independent_child(self):
        self.project["timeout"] = 0.2
        write_json(self.root / "projects" / "demo" / "project.json", self.project)
        jobrunner.rpc(
            self.root,
            {
                "action": "execution_templates",
                "project": "demo",
                "templates": self._template(seconds="60"),
            },
        )
        jobrunner.rpc(
            self.root,
            {
                "action": "execution_start",
                "project": "demo",
                "execution": "timeout",
                "template": "sleep",
            },
        )
        for _ in range(100):
            state = jobrunner.rpc(self.root, {"action": "execution_status", "execution": "timeout"})
            if state["status"] in jobrunner.TERMINAL:
                break
            time.sleep(0.02)
        self.assertEqual(state["status"], "timed_out")

    def test_cancel_accepted_at_completion_wins_final_commit(self):
        path = self.root / "executions" / "race"
        write_json(
            path / "request.json",
            {"argv": [sys.executable], "cwd": str(self.project_path), "timeout": 30},
        )
        write_json(
            path / "state.json",
            {"execution": "race", "project": "demo", "status": "starting"},
        )
        child = mock.Mock(pid=456)
        child.poll.return_value = 0

        def finished(*args, **kwargs):
            write_json(path / "cancel.json", {"requested_at": jobrunner.now()})
            return 0

        child.wait.side_effect = finished
        with (
            mock.patch.object(jobrunner.subprocess, "Popen", return_value=child),
            mock.patch.object(jobrunner, "_stamp", return_value="stamp"),
        ):
            jobrunner._run(self.root, "race")
        self.assertEqual(read_json(path / "state.json")["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
