import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness.app_bridge import AppBridge
from agent_harness.common import write_json
from agent_harness.desktop_runtime import DesktopRuntime


class ReceiverFreshnessTests(unittest.TestCase):
    def test_login_refreshes_only_running_receivers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = root / "receivers" / "device-1"
            receiver.mkdir(parents=True)
            (receiver.parent / "unrelated").mkdir()
            bridge = AppBridge.__new__(AppBridge)
            bridge.root = root
            with mock.patch("agent_harness.app_bridge.DesktopRuntime") as runtime:
                bridge._ensure_receivers()
            runtime.assert_called_once_with(receiver)
            runtime.return_value.start.assert_called_once_with(refresh_only=True)

    def test_same_version_with_changed_code_defers_until_no_work_can_start(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, root = folder / "code", folder / "receiver"
            source.mkdir()
            (source / "VERSION").write_text("2.0.0", encoding="utf-8")
            addon = source / "executors.py"
            addon.write_text("first", encoding="utf-8")
            runtime = DesktopRuntime(root)
            write_json(runtime.config_path, {"node": "test"})
            active = root / "projects" / "p" / "active.json"
            running = {"running": True}
            with (
                mock.patch("agent_harness.desktop_runtime.assets", return_value=source),
                mock.patch.object(runtime, "status", return_value=running),
                mock.patch.object(runtime, "_control", return_value={"stopping": False}) as control,
                mock.patch.object(runtime, "stop_and_wait") as stop,
            ):
                old = runtime._service_snapshot()
                write_json(runtime.control_path, {"assets": str(old)})
                self.assertEqual(runtime.start(refresh_only=True), running)
                control.assert_not_called()
                addon.write_text("second", encoding="utf-8")
                write_json(active, {"job": "some-job"})
                self.assertTrue(runtime.start(refresh_only=True)["refresh_pending"])
                control.assert_not_called()
                write_json(active, None)
                self.assertTrue(runtime.start(refresh_only=True)["refresh_pending"])
                control.assert_called_once_with("stop-if-idle", {})
                stop.assert_not_called()

    def test_refresh_does_not_start_a_stopped_receiver(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = DesktopRuntime(directory)
            write_json(runtime.config_path, {"node": "test"})
            with (
                mock.patch.object(runtime, "status", return_value={"running": False}),
                mock.patch.object(runtime, "_service_snapshot") as snapshot,
            ):
                self.assertFalse(runtime.start(refresh_only=True)["running"])
            snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
