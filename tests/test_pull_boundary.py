import tempfile
import unittest
from collaboration_store import Store
from broker import PullBackend
from panel import action
import worker


class BoundaryTests(unittest.TestCase):
    def test_default_panel_and_coordinator_never_construct_ssh_adapter(self):
        from panel import handler_for
        from coordinator import Coordinator

        with tempfile.TemporaryDirectory() as root:
            handler = handler_for(root)
            self.assertIsInstance(handler.backend, PullBackend)
            self.assertIsInstance(Coordinator(handler.store).backend, PullBackend)

    def test_central_configuration_rejected_before_reserving_agent(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            owner = store.signup("owner", "", bootstrap=True)
            backend = PullBackend(store)
            with self.assertRaises(PermissionError):
                action(store, owner, {"action": "configure-agent", "agent": "unknown"}, backend)
            with self.assertRaises(PermissionError):
                action(store, owner, {"action": "add-agent"}, backend)

    def test_inspection_keeps_read_only_sandbox_and_implementation_uses_workspace(self):
        p = {"model": "gpt-5.6-sol", "effort": "medium", "remote_path": "D:/project", "local_control": True}
        args = worker.build_command("codex.exe", p, None)
        self.assertNotIn("--approve-for-me", args)
        self.assertIn('approval_policy="never"', args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertNotIn("shell_tool", args)
        self.assertNotIn("unified_exec", args)
        p["_stage"] = "review"
        args = worker.build_command("codex.exe", p, None)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        p["_stage"] = "implement"
        args = worker.build_command("codex.exe", p, None)
        self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
        self.assertNotIn("shell_tool", args)


if __name__ == "__main__":
    unittest.main()
