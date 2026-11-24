import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness.app_bridge import AppBridge


class ReceiverFreshnessTests(unittest.TestCase):
    def test_a_receiver_deployed_from_an_older_build_is_restarted(self):
        root = Path(tempfile.mkdtemp())
        receiver = root / "receivers" / "device-1"
        receiver.mkdir(parents=True)
        (receiver / "VERSION").write_text("1.0.0", encoding="utf-8")
        (root / "VERSION").write_text("2.0.0", encoding="utf-8")
        bridge = AppBridge.__new__(AppBridge)
        bridge.root = root
        with mock.patch("agent_harness.desktop_runtime.version_path", return_value=root / "VERSION"):
            with mock.patch("agent_harness.desktop_runtime.DesktopRuntime") as runtime:
                bridge._ensure_receivers()
        runtime.assert_called_once()
        runtime.return_value.start.assert_called_once()

    def test_an_up_to_date_receiver_is_left_running(self):
        root = Path(tempfile.mkdtemp())
        receiver = root / "receivers" / "device-1"
        receiver.mkdir(parents=True)
        (receiver / "VERSION").write_text("2.0.0", encoding="utf-8")
        (root / "VERSION").write_text("2.0.0", encoding="utf-8")
        bridge = AppBridge.__new__(AppBridge)
        bridge.root = root
        with mock.patch("agent_harness.desktop_runtime.version_path", return_value=root / "VERSION"):
            with mock.patch("agent_harness.desktop_runtime.DesktopRuntime") as runtime:
                bridge._ensure_receivers()
        runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
