import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_harness.worker import _check_strict_guard, _strict_guard, _worktree_status


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


class StrictWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.root / "base"
        self.wt = self.root / "wt"
        self.base.mkdir()
        git(self.base, "init", "-q")
        git(self.base, "config", "user.email", "test@example.invalid")
        git(self.base, "config", "user.name", "Harness Test")
        (self.base / "allowed.txt").write_text("before", encoding="utf-8")
        (self.base / "src").mkdir()
        (self.base / "src" / "file.txt").write_text("before", encoding="utf-8")
        git(self.base, "add", ".")
        git(self.base, "commit", "-qm", "initial")
        git(self.base, "worktree", "add", "-q", str(self.wt), "-b", "test-wt")
        self.project = {
            "name": "p",
            "remote_path": str(self.wt),
            "model": "model",
            "effort": "medium",
            "timeout": 120,
            "strict_worktree": True,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_clean_baseline_and_allowed_file(self):
        guard = _strict_guard(self.project, {"stage": "implement", "allowed_files": "allowed.txt"})
        (self.wt / "allowed.txt").write_text("changed", encoding="utf-8")
        self.assertEqual(_check_strict_guard(guard), None)

    def test_dirty_baseline_rejected(self):
        (self.wt / "allowed.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean"):
            _strict_guard(self.project, {"stage": "implement", "allowed_files": "allowed.txt"})

    def test_disallowed_file_rejected(self):
        guard = _strict_guard(self.project, {"stage": "implement", "allowed_files": "allowed.txt"})
        (self.wt / "src" / "file.txt").write_text("changed", encoding="utf-8")
        self.assertIn("outside allowed_files", _check_strict_guard(guard))

    def test_head_change_rejected_even_when_clean(self):
        guard = _strict_guard(self.project, {"stage": "implement", "allowed_files": "allowed.txt"})
        (self.wt / "allowed.txt").write_text("commit", encoding="utf-8")
        git(self.wt, "add", "allowed.txt")
        git(self.wt, "commit", "-qm", "worker commit")
        self.assertIn("HEAD changed", _check_strict_guard(guard))

    def test_rename_tracks_both_paths(self):
        guard = _strict_guard(self.project, {"stage": "implement", "allowed_files": "allowed.txt\nrenamed.txt"})
        git(self.wt, "mv", "allowed.txt", "renamed.txt")
        self.assertEqual(set(_worktree_status(self.wt)), {"allowed.txt", "renamed.txt"})
        self.assertEqual(_check_strict_guard(guard), None)


if __name__ == "__main__":
    unittest.main()
