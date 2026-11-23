import unittest
from pathlib import Path
from unittest import mock

from agent_harness import sandbox_acl


class SandboxAclTests(unittest.TestCase):
    def test_grant_and_revoke_name_the_group_and_the_directory(self):
        grant = sandbox_acl.command("icacls", "CodexSandboxUsers", r"C:\Users\a\Documents", True)
        self.assertEqual(grant[0], "icacls")
        self.assertEqual(grant[1], r"C:\Users\a\Documents")
        self.assertIn("CodexSandboxUsers:(OI)(CI)M", grant)
        self.assertNotIn("/T", grant, "inherited entries need no recursive walk")

        revoke = sandbox_acl.command("icacls", "CodexSandboxUsers", r"C:\Users\a\Documents", False)
        self.assertEqual(revoke[2], "/remove:g")
        self.assertEqual(revoke[3], "CodexSandboxUsers")

    def test_nothing_happens_off_windows(self):
        run = mock.Mock()
        with mock.patch.object(sandbox_acl.sys, "platform", "linux"):
            self.assertFalse(sandbox_acl.apply(Path("."), True, run=run))
        run.assert_not_called()

    def test_a_missing_sandbox_group_is_not_granted(self):
        run = mock.Mock()
        with mock.patch.object(sandbox_acl.sys, "platform", "win32"):
            with mock.patch.object(sandbox_acl, "has_group", return_value=False):
                self.assertFalse(sandbox_acl.apply(Path("."), True, run=run))
        run.assert_not_called()

    def test_a_present_group_is_granted_once(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.object(sandbox_acl.sys, "platform", "win32"):
            with mock.patch.object(sandbox_acl, "has_group", return_value=True):
                self.assertTrue(sandbox_acl.apply(Path(r"C:\Users\a"), True, run=run))
        argv = run.call_args.args[0]
        self.assertIn("CodexSandboxUsers:(OI)(CI)M", argv)

    def test_the_granted_path_is_the_user_profile(self):
        self.assertEqual(sandbox_acl.granted_paths(home=r"C:\Users\a"), [Path(r"C:\Users\a")])


if __name__ == "__main__":
    unittest.main()
