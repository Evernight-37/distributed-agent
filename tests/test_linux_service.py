import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from linux_service import ServiceManager, build_unit, unit_path


class LinuxServiceTests(unittest.TestCase):
    def test_unit_quotes_paths_and_escapes_percent(self):
        unit = build_unit("/tmp/py %bin/python", "/tmp/a b/receiver.py", "/tmp/data root")
        self.assertIn("ExecStart=", unit)
        self.assertIn("%%bin", unit)
        self.assertIn("receiver.py", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("UMask=0077", unit)

    def test_control_characters_rejected(self):
        with self.assertRaises(ValueError):
            build_unit("/tmp/python\n", "/tmp/receiver.py", "/tmp/data")

    @patch("linux_service.shutil.which", return_value="/usr/bin/systemctl")
    @patch("linux_service.subprocess.run")
    def test_install_writes_unit_and_preserves_data(self, run, _which):
        run.return_value.returncode = 0
        run.return_value.stdout = "active\n"
        run.return_value.stderr = ""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            root = Path(directory) / "data"
            root.mkdir(parents=True)
            marker = root / "identity.json"
            marker.write_text("keep", encoding="utf-8")
            manager = ServiceManager(root, "/usr/bin/python3", "/opt/client/linux_receiver.py", home=home)
            result = manager.install()
            self.assertTrue(result["active"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            if os.name != "nt":
                self.assertEqual(manager.path.stat().st_mode & 0o777, 0o600)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(["systemctl", "--user", "enable", "agent-harness-receiver.service"], commands)
            self.assertIn(["systemctl", "--user", "restart", "agent-harness-receiver.service"], commands)

    @patch("linux_service.shutil.which", return_value="/usr/bin/systemctl")
    @patch("linux_service.subprocess.run")
    def test_uninstall_removes_only_unit(self, run, _which):
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            root = Path(directory) / "data"
            root.mkdir(parents=True)
            manager = ServiceManager(root, "/usr/bin/python3", "/opt/client/linux_receiver.py", home=home)
            manager.path.parent.mkdir(parents=True)
            manager.path.write_text("unit", encoding="utf-8")
            (root / "keep.txt").write_text("keep", encoding="utf-8")
            result = manager.uninstall()
            self.assertFalse(manager.path.exists())
            self.assertTrue((root / "keep.txt").exists())
            self.assertEqual(result["data_root"], str(root.resolve()))

    @patch("linux_service.shutil.which", return_value="/usr/bin/systemctl")
    @patch("linux_service.subprocess.run")
    def test_service_commands_are_user_scoped(self, run, _which):
        run.return_value.returncode = 0
        run.return_value.stdout = "active\n"
        run.return_value.stderr = ""
        with tempfile.TemporaryDirectory() as directory:
            manager = ServiceManager(Path(directory) / "data", "/usr/bin/python3", "/opt/receiver.py", home=directory)
            manager.start()
            manager.restart()
            manager.stop()
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(
                commands,
                [
                    ["systemctl", "--user", "start", "agent-harness-receiver.service"],
                    ["systemctl", "--user", "restart", "agent-harness-receiver.service"],
                    ["systemctl", "--user", "stop", "agent-harness-receiver.service"],
                ],
            )

    def test_unit_path_is_under_user_config(self):
        path = unit_path("/tmp/home")
        self.assertEqual(path.name, "agent-harness-receiver.service")
        self.assertEqual(path.parent.name, "user")
        self.assertEqual(path.parent.parent.name, "systemd")

    def test_receiver_logs_are_bounded_and_exclude_task_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            receiver = root / "receivers/device-one"
            receiver.mkdir(parents=True)
            (receiver / "desktop-service.stdout.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
            (receiver / "desktop-service.stderr.log").write_text("error-one\nerror-two\n", encoding="utf-8")
            (receiver / "report.txt").write_text("secret task report", encoding="utf-8")
            outside = root / "outside.log"
            outside.write_text("must not be exposed", encoding="utf-8")
            linked_receiver = root / "receivers/device-linked"
            linked_receiver.mkdir()
            try:
                (linked_receiver / "desktop-service.stdout.log").symlink_to(outside)
            except OSError:
                pass
            manager = ServiceManager(root, "/usr/bin/python3", "/opt/receiver.py", home=directory)
            result = manager.receiver_logs(lines=1)
            self.assertEqual(result[0]["stdout"], "three")
            self.assertEqual(result[0]["stderr"], "error-two")
            self.assertNotIn("secret task report", str(result))
            self.assertNotIn("must not be exposed", str(result))


if __name__ == "__main__":
    unittest.main()
