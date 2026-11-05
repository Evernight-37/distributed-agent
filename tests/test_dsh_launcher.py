"""The stable launcher that keeps the DeepSeek Harness entry upgrade-proof."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import dsh_mcp
from agent_harness.app_bridge import AppBridge


class CodexLauncherTests(unittest.TestCase):
    """The Codex entry must survive an upgrade too."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name) / "data"
        self.bridge = AppBridge(self.root)
        self.bridge.profile = {
            "id": "profile-one",
            "node": "device-local",
            "server": "http://127.0.0.1:8765",
        }
        self.bridge.identity = {"account_id": "local"}
        self.addCleanup(self._temp.cleanup)

    def test_the_registered_command_is_a_stable_launcher(self):
        command = self.bridge._mcp_launch()
        self.assertEqual(len(command), 1)
        launcher = Path(command[0])
        self.assertEqual(launcher.parent, (self.root / "mcp").resolve())
        self.assertTrue(launcher.is_file())
        text = launcher.read_text(encoding="utf-8")
        self.assertIn("mcp_adapter.py", text)
        self.assertIn("--grant", text)

    def test_the_launcher_is_rewritten_when_the_runtime_moves(self):
        first = self.bridge._mcp_launch()[0]
        launcher = Path(first)
        launcher.write_text("stale\n", encoding="utf-8")
        second = self.bridge._mcp_launch()[0]
        self.assertEqual(first, second)
        self.assertIn("mcp_adapter.py", launcher.read_text(encoding="utf-8"))

    def test_an_entry_of_ours_from_an_older_layout_is_refreshed(self):
        grants = [str(self.bridge._mcp_grant_path())]
        transport = {
            "type": "stdio",
            "command": "C:\\old\\service-runtime\\abc\\runtime\\python.exe",
            "args": ["-X", "utf8", "C:\\old\\mcp_adapter.py", "--grant", grants[0]],
        }
        self.assertTrue(self.bridge._mcp_entry_is_ours(transport))
        self.assertFalse(self.bridge._mcp_entry_is_ours({"type": "stdio", "command": "other.exe", "args": []}))


class DshLauncherTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name) / "data"
        self.home = Path(self._temp.name) / "dsh-home"
        (self.home / "profiles" / "desktop").mkdir(parents=True)
        self.bridge = AppBridge(self.root)
        self.bridge.profile = {
            "id": "profile-one",
            "node": "device-local",
            "server": "http://127.0.0.1:8765",
        }
        self.bridge.identity = {"account_id": "local"}
        self._env = mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._temp.cleanup)

    def test_the_path_is_stable_and_does_not_collide_with_codex(self):
        path = self.bridge._dsh_mcp_launcher_path()
        suffix = ".cmd" if os.name == "nt" else ".sh"
        self.assertEqual(path.name, "launch-profile-one-dsh" + suffix)
        # Reading the path must not create anything.
        self.assertFalse(path.exists())

    def test_the_launcher_runs_the_current_adapter_and_grant(self):
        path = self.bridge._ensure_dsh_mcp_launcher()
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("@echo off") if os.name == "nt" else text.startswith("#!/bin/sh"))
        self.assertIn("mcp_adapter.py", text)
        self.assertIn("--grant", text)
        self.assertIn("profile-one", text)
        if os.name != "nt":
            self.assertTrue(path.stat().st_mode & 0o100)

    def test_refreshing_leaves_the_same_bytes_alone(self):
        path = self.bridge._ensure_dsh_mcp_launcher()
        before = path.stat().st_mtime_ns
        self.bridge._ensure_dsh_mcp_launcher()
        self.assertEqual(before, path.stat().st_mtime_ns)

    def test_a_stale_launcher_is_rewritten(self):
        path = self.bridge._dsh_mcp_launcher_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old launcher\n", encoding="utf-8")
        self.bridge._ensure_dsh_mcp_launcher()
        self.assertIn("mcp_adapter.py", path.read_text(encoding="utf-8"))

    def test_status_repairs_an_entry_pointing_at_a_dead_command(self):
        launch = [str(self.bridge._dsh_mcp_launcher_path())]
        name = self.bridge._dsh_mcp_name()
        dsh_mcp.install(self.home, launch, name)
        fresh = self.bridge._dsh_mcp_status()
        self.assertTrue(fresh["configured"])
        self.assertFalse(fresh["stale"])
        self.assertFalse(fresh["repaired"])

        # An entry left behind by a moved data directory points at nothing; it is
        # re-pointed instead of only being reported as stale.
        dsh_mcp.install(self.home, [r"C:\old\service-runtime\abc\runtime\python.exe"], name)
        healed = self.bridge._dsh_mcp_status()
        self.assertTrue(healed["configured"])
        self.assertTrue(healed["repaired"])
        self.assertFalse(healed["stale"])
        self.assertEqual(dsh_mcp.recorded_launch(self.home), str(self.bridge._dsh_mcp_launcher_path()))

    def test_status_reports_the_launcher_it_expects(self):
        state = self.bridge._dsh_mcp_status()
        self.assertFalse(state["configured"])
        self.assertEqual(state["profile"], "desktop")
        self.assertEqual(state["launcher"], str(self.bridge._dsh_mcp_launcher_path()))


if __name__ == "__main__":
    unittest.main()
