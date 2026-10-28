import threading
import time
import unittest

from agent_harness.event_pump import DesktopEventPump, notify_webview


class EventPumpTests(unittest.TestCase):
    def test_change_notifies_without_blocking_caller(self):
        release = threading.Event()
        notified = threading.Event()

        def wait(_identity, _server, cursor, _seconds):
            release.wait(1)
            return {"cursor": cursor + 1, "changed": True}

        pump = DesktopEventPump(wait, lambda *_: notified.set())
        started = time.monotonic()
        pump.update({"account_id": "a", "public_key": "p"}, ["https://one.invalid"])
        self.assertLess(time.monotonic() - started, 0.2)
        release.set()
        self.assertTrue(notified.wait(1))
        pump.stop()

    def test_generation_change_discards_stale_result(self):
        release = threading.Event()
        notifications = []

        def wait(_identity, server, cursor, _seconds):
            if server.endswith("old.invalid"):
                release.wait(1)
            else:
                time.sleep(0.01)
            return {"cursor": cursor + 1, "changed": True}

        pump = DesktopEventPump(wait, lambda cursor, server: notifications.append((cursor, server)))
        identity = {"account_id": "a", "public_key": "p"}
        pump.update(identity, ["https://old.invalid"])
        pump.update(identity, ["https://new.invalid"])
        release.set()
        deadline = time.monotonic() + 1
        while not notifications and time.monotonic() < deadline:
            time.sleep(0.01)
        pump.stop()
        self.assertTrue(notifications)
        self.assertTrue(all(server.endswith("new.invalid") for _, server in notifications))

    def test_webview_callback_is_json_encoded_and_close_race_is_safe(self):
        class Window:
            def __init__(self):
                self.script = None

            def evaluate_js(self, script):
                self.script = script

        window = Window()
        notify_webview(window, 3, 'https://example.invalid/";alert(1)//')
        self.assertIn("window.__harnessEvent?.(3,", window.script)
        self.assertIn('\\";alert(1)//', window.script)
        notify_webview(None, 4, "https://example.invalid")


if __name__ == "__main__":
    unittest.main()
