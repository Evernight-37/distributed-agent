"""Session actions: background keeps services alive only when a tray can restore."""

import tempfile
import unittest
from pathlib import Path

from agent_harness.desktop_api import DesktopSessionActions


class BackgroundTests(unittest.TestCase):
    def bridge(self, root, on_background=None):
        class Bridge:
            identity = {"account_id": "account"}
            profile = {"server": "http://127.0.0.1:8765"}
            window = None
            allow_window_close = False
            on_background = None

            def __init__(self, root):
                self.root = root

            def _host_state_root(self):
                return self.root

        bridge = Bridge(root)
        bridge.on_background = on_background
        return bridge

    def test_background_hides_the_window_instead_of_destroying_it(self):
        with tempfile.TemporaryDirectory() as folder:
            hidden = []
            bridge = self.bridge(Path(folder), on_background=lambda: hidden.append(True))
            result = DesktopSessionActions(bridge).invoke("app-background", {})
            self.assertEqual(result, {"background": True})
            self.assertEqual(hidden, [True])
            self.assertFalse(bridge.allow_window_close)

    def test_without_a_tray_the_window_closes_and_services_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = self.bridge(Path(folder))
            result = DesktopSessionActions(bridge).invoke("app-background", {})
            self.assertEqual(result, {"background": True})
            self.assertTrue(bridge.allow_window_close)


if __name__ == "__main__":
    unittest.main()
