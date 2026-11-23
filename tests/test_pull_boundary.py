import tempfile
import unittest
import json
import os
from pathlib import Path
from unittest.mock import patch
from agent_harness.broker import PullBackend
from agent_harness import worker


class BoundaryTests(unittest.TestCase):
    def test_owner_selected_workspace_accepts_implementation_without_linked_worktree(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(worker, "ROOT", Path(folder)),
        ):
            workspace = Path(folder) / "workspace"
            workspace.mkdir()
            worker.rpc(
                {
                    "action": "register",
                    "project": {
                        "name": "local",
                        "remote_path": str(workspace),
                        "model": "gpt-5.6-sol",
                        "effort": "low",
                        "timeout": 30,
                        "local_control": True,
                    },
                    "memory": {"revision": 0},
                }
            )
            with patch.object(worker, "launch_supervisor", return_value=os.getpid()) as launch:
                result = worker.rpc(
                    {
                        "action": "start",
                        "project": "local",
                        "job": "local-edit",
                        "prompt": json.dumps({"stage": "implement", "allowed_files": "result.txt"}),
                    }
                )
            launch.assert_called_once_with("local-edit")
            self.assertEqual(result["status"], "starting")
            config = worker.read_json(worker.job_dir("local-edit") / "request.json")
            self.assertEqual(config["project"]["remote_path"], str(workspace.resolve()))

    def test_default_panel_and_coordinator_never_construct_ssh_adapter(self):
        from agent_harness.panel import handler_for
        from agent_harness.coordinator import Coordinator

        with tempfile.TemporaryDirectory() as root:
            handler = handler_for(root)
            self.assertIsInstance(handler.backend, PullBackend)
            self.assertIsInstance(Coordinator(handler.store).backend, PullBackend)

    def test_every_stage_keeps_the_workspace_writable(self):
        """Capability comes from the executor; the stage only names the intent."""
        project = {
            "model": "gpt-5.6-sol",
            "effort": "medium",
            "remote_path": "D:/project",
            "local_control": True,
        }
        for stage in (None, "discuss", "review", "implement"):
            args = worker.build_command("codex.exe", dict(project, _stage=stage), None)
            self.assertNotIn("--approve-for-me", args)
            self.assertIn('approval_policy="never"', args)
            self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write", str(stage))
            self.assertNotIn("shell_tool", args)
            self.assertNotIn("unified_exec", args)
