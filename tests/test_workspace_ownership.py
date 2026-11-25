import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_harness.workspace_ownership import DIRTY_WORKTREE, claim_worktree


@unittest.skipUnless(shutil.which("git"), "Git is required")
class WorktreeOwnershipTests(unittest.TestCase):
    def test_same_work_can_continue_and_a_different_work_waits_until_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            child = repo / "subdirectory"
            child.mkdir(parents=True)
            git = shutil.which("git")
            subprocess.run([git, "-C", str(repo), "init", "-q"], check=True)
            changed = child / "uncommitted.txt"

            def changed_paths(checkout):
                self.assertEqual(checkout, repo.resolve())
                return [changed] if changed.exists() else []

            claim_worktree(root, repo, "login", git, changed_paths)
            changed.write_text("work in progress", encoding="utf-8")
            claim_worktree(root, child, "login", git, changed_paths)
            with self.assertRaisesRegex(ValueError, DIRTY_WORKTREE):
                claim_worktree(root, child, "packaging", git, changed_paths)
            changed.unlink()
            claim_worktree(root, child, "packaging", git, changed_paths)
            changed.write_text("packaging changes", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, DIRTY_WORKTREE):
                claim_worktree(root, repo, "login", git, changed_paths)
