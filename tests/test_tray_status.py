import threading
import unittest
from unittest.mock import Mock

from agent_harness.tray_status import TrayStatus


class TrayStatusTests(unittest.TestCase):
    def setUp(self):
        self.tray = Mock()
        self.account = "account-1"
        self.background = threading.Event()
        self.status = TrayStatus(
            self.tray,
            lambda: self.account,
            lambda: (True, [{"running": True}, {"running": False}]),
            self.background,
        )

    def attention(self, cursor, unread, latest, *, host="local", account="account-1"):
        self.status.attention(
            account, host, cursor, {"unread_count": unread, "latest_unread_id": latest, "service_id": "service-1"}
        )

    def test_tray_reports_local_services_and_backlog_without_alerting_on_startup(self):
        self.background.set()
        self.attention(1, 2, 10)
        self.assertEqual(self.status.title(), "本机 Hub 运行中 · 接收端 1/2 运行 · 2 条未读")
        self.tray.notify.assert_not_called()
        self.attention(2, 3, 11)
        self.tray.notify.assert_called_once_with()
        self.attention(2, 3, 11, host="alias")
        self.tray.notify.assert_called_once_with()
        self.assertIn("3 条未读", self.status.title())
        self.attention(3, 0, 0)
        self.assertNotIn("未读", self.status.title())

    def test_foreground_and_old_account_events_do_not_show_notifications(self):
        self.attention(1, 0, 0)
        self.attention(2, 1, 10)
        self.tray.notify.assert_not_called()
        self.background.set()
        self.account = "account-2"
        self.attention(3, 2, 11)
        self.assertNotIn("未读", self.status.title())
        self.tray.notify.assert_not_called()
