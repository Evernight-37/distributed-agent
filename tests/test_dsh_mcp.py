import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import dsh_mcp, executors
from agent_harness.common import write_json

LAUNCH = [r"C:\python.exe", "-X", "utf8", r"C:\app\mcp_adapter.py", "--grant", r"C:\data\grant.json"]
NAME = "harness-abc12345"
PROJECT = {"model": "deepseek-flash", "effort": "medium", "local_control": True}


def make_home(folder, profiles=("desktop", "headless")):
    """Materialize a DeepSeek Harness home the way its launcher does."""
    home = Path(folder) / "home"
    for name in profiles:
        (home / "profiles" / name).mkdir(parents=True, exist_ok=True)
        (home / "profiles" / name / "package.json").write_text("{}", encoding="utf-8")
    return home


class HomeResolutionTests(unittest.TestCase):
    def test_dsh_home_environment_wins(self):
        with mock.patch.dict(os.environ, {"DSH_HOME": r"D:\custom-home"}):
            self.assertEqual(dsh_mcp.home(), Path(r"D:\custom-home"))
            self.assertEqual(
                dsh_mcp.patch_path(root_home=r"D:\custom-home"),
                Path(r"D:\custom-home") / "profiles" / "desktop" / "cordis.patch.yml",
            )

    def test_blank_override_is_ignored(self):
        with mock.patch.dict(os.environ, {"DSH_HOME": "   "}):
            self.assertEqual(dsh_mcp.home(), Path.home() / ".dsh")

    def test_default_target_is_the_desktop_profile(self):
        self.assertEqual(dsh_mcp.DEFAULT_PROFILE, "desktop")
        self.assertIn(str(Path("profiles") / "desktop"), str(dsh_mcp.patch_path(root_home="X")))

    def test_server_name_is_contract_legal_and_stable(self):
        name = dsh_mcp.server_name("agent-harness-profile-0123456789abcdef")
        self.assertRegex(name, r"^[A-Za-z0-9_-]{1,32}$")
        self.assertLessEqual(len(name), 32)
        self.assertEqual(name, dsh_mcp.server_name("agent-harness-profile-0123456789abcdef"))
        self.assertNotEqual(name, dsh_mcp.server_name("agent-harness-profile-fedcba9876543210"))


class EntryTests(unittest.TestCase):
    def test_entry_is_one_json_line_with_the_expected_shape(self):
        text = dsh_mcp.line(LAUNCH, NAME)
        self.assertNotIn("\n", text)
        self.assertTrue(text.startswith("- {"))
        payload = json.loads(text[2:])
        inserted = payload["insert"][0]
        self.assertEqual(inserted["id"], dsh_mcp.ENTRY_ID)
        self.assertEqual(inserted["name"], dsh_mcp.SERVER_MODULE)
        self.assertEqual(inserted["config"]["serverName"], NAME)
        self.assertEqual(inserted["config"]["transport"], "stdio")
        self.assertEqual(inserted["config"]["command"], LAUNCH[0])
        self.assertEqual(inserted["config"]["args"], LAUNCH[1:])

    def test_windows_paths_are_escaped_for_yaml_double_quotes(self):
        text = dsh_mcp.line([r"C:\Program Files\python.exe"], NAME)
        self.assertIn(r"C:\\Program Files\\python.exe", text)

    def test_empty_launch_is_rejected(self):
        with self.assertRaises(ValueError):
            dsh_mcp.line([], NAME)


class RepairTests(unittest.TestCase):
    def _installed(self, home, launch):
        dsh_mcp.install(home, launch, NAME)
        return dsh_mcp.patch_path(dsh_mcp.DEFAULT_PROFILE, home)

    def test_a_moved_launcher_is_re_pointed(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            old = [str(Path(folder) / "old" / "launch.cmd")]
            patch = self._installed(home, old)
            self.assertEqual(dsh_mcp.recorded_launch(home), old[0])
            self.assertTrue(patch.is_file())

            new = [str(Path(folder) / "new" / "launch.cmd")]
            Path(new[0]).parent.mkdir(parents=True)
            Path(new[0]).write_text("@echo off\n", encoding="utf-8")

            result = dsh_mcp.repair(home, new, NAME)
            self.assertTrue(result["repaired"])
            self.assertFalse(result["stale"])
            self.assertEqual(dsh_mcp.recorded_launch(home), new[0])

    def test_a_working_command_that_is_not_ours_is_left_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            home_launch = Path(folder) / "elsewhere" / "launch.cmd"
            home_launch.parent.mkdir(parents=True)
            home_launch.write_text("@echo off\n", encoding="utf-8")
            self._installed(home, [str(home_launch)])

            new = [str(Path(folder) / "new" / "launch.cmd")]
            Path(new[0]).parent.mkdir(parents=True)
            Path(new[0]).write_text("@echo off\n", encoding="utf-8")

            result = dsh_mcp.repair(home, new, NAME)
            self.assertFalse(result["repaired"])
            self.assertIn("另一个仍然存在", result["reason"])

    def test_a_missing_entry_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            result = dsh_mcp.repair(home, [str(Path(folder) / "launch.cmd")], NAME)
            self.assertFalse(result["repaired"])
            self.assertFalse(dsh_mcp.status(home, [str(Path(folder) / "launch.cmd")], NAME)["installed"])


class InstallTests(unittest.TestCase):
    def _install(self, home, launch=None, probe=None):
        return dsh_mcp.install(home, launch or LAUNCH, NAME, probe=probe)

    def test_install_targets_the_desktop_profile_only(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            result = self._install(home)
            self.assertEqual(result["profile"], "desktop")
            self.assertIn(str(Path("profiles") / "desktop"), result["path"])
            self.assertTrue(dsh_mcp.patch_path(root_home=home).is_file())
            self.assertFalse(dsh_mcp.patch_path("headless", home).exists())
            self.assertTrue(dsh_mcp.status(home, LAUNCH, NAME)["installed"])

    def test_a_missing_profile_is_never_created(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder, profiles=("headless",))
            with self.assertRaises(ValueError) as raised:
                self._install(home)
            self.assertIn("桌面应用", str(raised.exception))
            self.assertFalse((home / "profiles" / "desktop").exists())

    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            self._install(home)
            self._install(home)
            text = dsh_mcp.patch_path(root_home=home).read_text(encoding="utf-8")
            self.assertEqual(text.count(dsh_mcp.MARKER), 1)
            self.assertEqual(text.count(dsh_mcp.ENTRY_ID), 1)

    def test_gui_written_entries_and_comments_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            path = dsh_mcp.patch_path(root_home=home)
            original = (
                "# Your patch layer for this dsh profile\n"
                "- id: ui-chat\n"
                '  name: "@deepseek-ai/dsh-client-ui-chat"\n'
                "  config:\n"
                "    transcriptView: standard\n"
            )
            path.write_text(original, encoding="utf-8")
            self._install(home)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(original))
            self.assertIn(dsh_mcp.MARKER, text)

    def test_bare_empty_list_is_replaced_not_appended_to(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            path = dsh_mcp.patch_path(root_home=home)
            path.write_text("# keep me\n[]\n", encoding="utf-8")
            self._install(home)
            text = path.read_text(encoding="utf-8")
            self.assertIn("# keep me", text)
            self.assertNotIn("[]", text)

    def test_foreign_flow_style_file_is_never_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            path = dsh_mcp.patch_path(root_home=home)
            original = '[{"id": "user-entry"}]\n'
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                self._install(home)
            self.assertIn("\u5757\u72b6\u5217\u8868", str(raised.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_stale_entry_is_refreshed(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            self._install(home)
            other = [r"D:\other\python.exe"]
            self.assertTrue(dsh_mcp.status(home, other, NAME)["stale"])
            self._install(home, other)
            self.assertFalse(dsh_mcp.status(home, other, NAME)["stale"])
            text = dsh_mcp.patch_path(root_home=home).read_text(encoding="utf-8")
            self.assertEqual(text.count(dsh_mcp.MARKER), 1)
            self.assertIn(r"D:\\other\\python.exe", text)

    def test_failed_probe_rolls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            path = dsh_mcp.patch_path(root_home=home)
            original = "- id: ui-chat\n"
            path.write_text(original, encoding="utf-8")

            def probe():
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError) as raised:
                self._install(home, probe=probe)
            self.assertIn("\u56de\u6eda", str(raised.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), original)


class RemoveTests(unittest.TestCase):
    def test_removal_restores_the_previous_document(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            path = dsh_mcp.patch_path(root_home=home)
            original = "# Your patch layer\n- id: ui-chat\n  config:\n    keep: true\n"
            path.write_text(original, encoding="utf-8")
            dsh_mcp.install(home, LAUNCH, NAME)
            result = dsh_mcp.remove(root_home=home)
            self.assertTrue(result["removed"])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_removal_of_an_empty_document_leaves_a_valid_list(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            dsh_mcp.install(home, LAUNCH, NAME)
            dsh_mcp.remove(root_home=home)
            self.assertEqual(dsh_mcp.patch_path(root_home=home).read_text(encoding="utf-8").strip(), "[]")

    def test_removal_without_a_managed_entry_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            path = dsh_mcp.patch_path(root_home=home)
            path.write_text("- id: ui-chat\n", encoding="utf-8")
            result = dsh_mcp.remove(root_home=home)
            self.assertFalse(result["removed"])
            self.assertEqual(path.read_text(encoding="utf-8"), "- id: ui-chat\n")

    def test_removal_keeps_user_lines_that_follow_the_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            home = make_home(folder)
            path = dsh_mcp.patch_path(root_home=home)
            path.write_text("- id: ui-chat\n", encoding="utf-8")
            dsh_mcp.install(home, LAUNCH, NAME)
            with path.open("a", encoding="utf-8") as stream:
                stream.write("- id: later-entry\n")
            dsh_mcp.remove(root_home=home)
            self.assertEqual(path.read_text(encoding="utf-8"), "- id: ui-chat\n- id: later-entry\n")


class TurnPatchTests(unittest.TestCase):
    def test_dispatched_turns_do_not_mention_the_collaboration_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "receiver"
            home = make_home(folder)
            scratch = root / "job"
            write_json(root / "executor.json", {"executor": "dsh"})
            with mock.patch.dict(os.environ, {"DSH_HOME": str(home)}):
                dsh_mcp.install(home, LAUNCH, NAME)
                executors.command(root, "dsh", dict(PROJECT, remote_path=str(root)), None, scratch=scratch)
                patch = json.loads((scratch / "dsh-patch.json").read_text(encoding="utf-8"))
            # The entry lives in the desktop profile; the one-shot headless turn
            # never sees it, so no per-turn disable is needed.
            self.assertEqual(
                [item["id"] for item in patch],
                ["agent-default-model", "approval", "permission", "credentials"],
            )


if __name__ == "__main__":
    unittest.main()
