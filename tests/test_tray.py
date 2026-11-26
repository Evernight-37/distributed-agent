import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_harness.tray import Tray


class NativeIcon:
    HAS_NOTIFICATION = True

    def __init__(self, name, image, title, menu):
        self.title = title
        self.visible = False
        self.allow_setup = threading.Event()
        self.allow_setup.set()
        self.stopped = threading.Event()
        self.notifications = []

    def run(self, setup):
        if self.allow_setup.wait(2):
            setup(self)
        self.stopped.wait(2)

    def notify(self, message, title):
        self.notifications.append((message, title))

    def stop(self):
        self.visible = False
        self.stopped.set()


class TrayTests(unittest.TestCase):
    def setUp(self):
        pystray = SimpleNamespace(
            Icon=NativeIcon,
            Menu=lambda *items: items,
            MenuItem=lambda label, handler, **kwargs: (label, handler),
        )
        with patch.dict(sys.modules, {"pystray": pystray}), patch("agent_harness.tray._image", return_value=object()):
            self.tray = Tray("Foolship", lambda: None, lambda: None)
        self.icon = self.tray._icon

    def tearDown(self):
        self.tray.stop()
        self.icon.allow_setup.set()
        if self.tray._thread:
            self.tray._thread.join(timeout=2)

    def test_ready_tooltip_and_fixed_notification_text(self):
        self.assertFalse(self.tray.notify())
        self.assertFalse(self.tray.set_status("正在启动"))
        self.assertTrue(self.tray.start())
        self.assertTrue(self.icon.visible)
        self.assertEqual(self.icon.title, "Foolship · 正在启动")
        self.assertTrue(self.tray.set_status("后台运行 · 1 条未读"))
        self.assertEqual(self.icon.title, "Foolship · 后台运行 · 1 条未读")
        self.assertTrue(self.tray.notify())
        self.assertTrue(self.tray.notify("approval"))
        self.assertEqual(
            self.icon.notifications,
            [
                ("有新的项目消息。打开 Foolship 查看。", "Foolship"),
                ("有新的审批请求。打开 Foolship 查看。", "Foolship"),
            ],
        )
        self.tray.stop()
        self.assertFalse(self.tray.ready)
        self.assertFalse(self.tray.notify())
        self.assertTrue(self.icon.stopped.is_set())

    def test_timing_out_and_stopping_before_setup_cannot_show_icon_late(self):
        self.icon.allow_setup.clear()
        self.assertFalse(self.tray.start(timeout=0))
        self.tray.stop()
        self.icon.allow_setup.set()
        self.assertTrue(self.icon.stopped.wait(1))
        self.assertFalse(self.icon.visible)
        self.assertFalse(self.tray.ready)

    def test_tooltip_respects_windows_utf16_limit(self):
        self.assertTrue(self.tray.start())
        self.assertTrue(self.tray.set_status("🚀" * 100))
        self.assertLessEqual(len(self.icon.title.encode("utf-16-le")), 254)
