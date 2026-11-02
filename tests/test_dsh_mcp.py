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


class HomeResolutionTests(unittest.TestCase):
    def test_dsh_home_environment_wins(self):
        with mock.patch.dict(os.environ, {"DSH_HOME": r"D:\custom-home"}):
            self.assertEqual(dsh_mcp.home(), Path(r"D:\custom-home"))
            self.assertEqual(dsh_mcp.patch_path(), Path(r"D:\custom-home") / "cordis.patch.yml")

    def test_blank_override_is_ignored(self):
        with mock.patch.dict(os.environ, {"DSH_HOME": "   "}):
            self.assertEqual(dsh_mcp.home(), Path.home() / ".dsh")

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


class InstallTests(unittest.TestCase):
    def _install(self, home, launch=None, probe=None):
        return dsh_mcp.install(Path(home), launch or LAUNCH, NAME, probe=probe)

    def test_missing_file_is_created(self):
        with tempfile.TemporaryDirectory() as home:
            self._install(home)
            text = dsh_mcp.patch_path(home).read_text(encoding="utf-8")
            self.assertIn(dsh_mcp.MARKER, text)
            self.assertTrue(dsh_mcp.status(home, LAUNCH, NAME)["installed"])

    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as home:
            self._install(home)
            self._install(home)
            text = dsh_mcp.patch_path(home).read_text(encoding="utf-8")
            self.assertEqual(text.count(dsh_mcp.MARKER), 1)
            self.assertEqual(text.count(dsh_mcp.ENTRY_ID), 1)

    def test_existing_entries_are_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as home:
            path = dsh_mcp.patch_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            original = "- id: my-entry\n  config:\n    keep: true\n"
            path.write_text(original, encoding="utf-8")
            self._install(home)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(original))
            self.assertIn(dsh_mcp.MARKER, text)

    def test_bare_empty_list_is_replaced_not_appended_to(self):
        with tempfile.TemporaryDirectory() as home:
            path = dsh_mcp.patch_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# keep me\n[]\n", encoding="utf-8")
            self._install(home)
            text = path.read_text(encoding="utf-8")
            self.assertIn("# keep me", text)
            self.assertNotIn("[]", text)

    def test_foreign_flow_style_file_is_never_rewritten(self):
        with tempfile.TemporaryDirectory() as home:
            path = dsh_mcp.patch_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            original = '[{"id": "user-entry"}]\n'
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                self._install(home)
            self.assertIn("\u5757\u72b6\u5217\u8868", str(raised.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_stale_entry_is_refreshed(self):
        with tempfile.TemporaryDirectory() as home:
            self._install(home)
            other = [r"D:\other\python.exe"]
            self.assertTrue(dsh_mcp.status(home, other, NAME)["stale"])
            self._install(home, other)
            self.assertFalse(dsh_mcp.status(home, other, NAME)["stale"])
            text = dsh_mcp.patch_path(home).read_text(encoding="utf-8")
            self.assertEqual(text.count(dsh_mcp.MARKER), 1)
            self.assertIn(r"D:\\other\\python.exe", text)

    def test_failed_probe_rolls_back(self):
        with tempfile.TemporaryDirectory() as home:
            path = dsh_mcp.patch_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            original = "- id: my-entry\n"
            path.write_text(original, encoding="utf-8")

            def probe():
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError) as raised:
                self._install(home, probe=probe)
            self.assertIn("\u56de\u6eda", str(raised.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_failed_probe_on_a_new_file_removes_it(self):
        with tempfile.TemporaryDirectory() as home:

            def probe():
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                self._install(home, probe=probe)
            self.assertFalse(dsh_mcp.patch_path(home).exists())


class RemoveTests(unittest.TestCase):
    def test_removal_restores_the_previous_document(self):
        with tempfile.TemporaryDirectory() as home:
            path = dsh_mcp.patch_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            original = "- id: my-entry\n  config:\n    keep: true\n"
            path.write_text(original, encoding="utf-8")
            dsh_mcp.install(Path(home), LAUNCH, NAME)
            result = dsh_mcp.remove(home)
            self.assertTrue(result["removed"])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_removal_of_an_empty_document_leaves_a_valid_list(self):
        with tempfile.TemporaryDirectory() as home:
            dsh_mcp.install(Path(home), LAUNCH, NAME)
            dsh_mcp.remove(home)
            self.assertEqual(dsh_mcp.patch_path(home).read_text(encoding="utf-8").strip(), "[]")

    def test_removal_without_a_managed_entry_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as home:
            path = dsh_mcp.patch_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("- id: my-entry\n", encoding="utf-8")
            result = dsh_mcp.remove(home)
            self.assertFalse(result["removed"])
            self.assertEqual(path.read_text(encoding="utf-8"), "- id: my-entry\n")

    def test_removal_keeps_user_lines_that_follow_the_entry(self):
        with tempfile.TemporaryDirectory() as home:
            path = dsh_mcp.patch_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("- id: my-entry\n", encoding="utf-8")
            dsh_mcp.install(Path(home), LAUNCH, NAME)
            with path.open("a", encoding="utf-8") as stream:
                stream.write("- id: later-entry\n")
            dsh_mcp.remove(home)
            self.assertEqual(path.read_text(encoding="utf-8"), "- id: my-entry\n- id: later-entry\n")


class TurnPatchTests(unittest.TestCase):
    def _turn_patch(self, root, scratch):
        executors.command(
            root,
            "dsh",
            dict(PROJECT, remote_path=str(root)),
            None,
            scratch=scratch,
        )
        return json.loads((scratch / "dsh-patch.json").read_text(encoding="utf-8"))

    def test_dispatched_turns_disable_the_collaboration_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "receiver"
            home = Path(folder) / "home"
            scratch = root / "job"
            write_json(root / "executor.json", {"executor": "dsh"})
            with mock.patch.dict(os.environ, {"DSH_HOME": str(home)}):
                without_entry = self._turn_patch(root, scratch)
                self.assertNotIn(dsh_mcp.ENTRY_ID, [item["id"] for item in without_entry])

                dsh_mcp.install(home, LAUNCH, NAME)
                with_entry = self._turn_patch(root, scratch)
                self.assertEqual(with_entry[0], {"id": dsh_mcp.ENTRY_ID, "disabled": True})
                self.assertEqual(
                    [item["id"] for item in with_entry[1:]],
                    ["agent-default-model", "approval", "permission", "credentials"],
                )


if __name__ == "__main__":
    unittest.main()
