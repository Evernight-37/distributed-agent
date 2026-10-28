"""Desktop permission checks must not open console windows on Windows."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_harness.app_bridge import AppBridge
from agent_harness.desktop_service import windows_user_sid
from agent_harness.host_control import protect


@unittest.skipUnless(os.name == "nt", "Windows console creation flags")
class NoConsoleTests(unittest.TestCase):
    def test_app_profile_acl_commands_are_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = AppBridge(Path(directory) / "app")
            with (
                patch("subprocess.check_output", return_value='"user","S-1-5-21-123"') as who,
                patch("subprocess.run", return_value=Mock(returncode=0)) as acl,
            ):
                bridge._secure(Path(directory) / "profile.json")
            self.assertEqual(who.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertEqual(acl.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_receiver_and_host_acl_commands_are_hidden(self):
        with patch("subprocess.check_output", return_value=b'"user","S-1-5-21-123"') as who:
            self.assertEqual(windows_user_sid(), "S-1-5-21-123")
        self.assertEqual(who.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        with (
            patch(
                "agent_harness.desktop_service.windows_user_sid",
                return_value="S-1-5-21-123",
            ),
            patch("subprocess.run", return_value=Mock(returncode=0)) as acl,
        ):
            protect(Path("example.json"))
        self.assertEqual(acl.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
