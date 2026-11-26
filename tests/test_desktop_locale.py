import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_harness import desktop_locale
from agent_harness.app_bridge import AppBridge
from agent_harness.tray import Tray
from agent_harness.tray_status import TrayStatus


class DesktopLocaleTests(unittest.TestCase):
    def test_follows_native_display_language_when_available(self):
        for platform, query, expected in (
            ("win32", "_windows_language", "zh-TW"),
            ("darwin", "_mac_language", "en-GB"),
        ):
            with (
                self.subTest(platform=platform),
                patch.object(sys, "platform", platform),
                patch.object(desktop_locale, query, return_value=expected),
                patch.dict("os.environ", {"LC_ALL": "ja_JP.UTF-8"}, clear=True),
            ):
                self.assertEqual(desktop_locale.system_language(), expected)

    def test_falls_back_to_environment_and_handles_missing_language(self):
        for platform in ("win32", "darwin", "linux"):
            with (
                self.subTest(platform=platform),
                patch.object(sys, "platform", platform),
                patch.object(desktop_locale, "_windows_language", side_effect=OSError),
                patch.object(desktop_locale, "_mac_language", side_effect=ImportError),
                patch.dict("os.environ", {"LANGUAGE": "C:zh_HK:en", "LANG": "en_US.UTF-8"}, clear=True),
            ):
                self.assertEqual(desktop_locale.system_language(), "zh-HK")
        with patch.object(sys, "platform", "linux"), patch.dict("os.environ", {"LANG": "C.UTF-8"}, clear=True):
            self.assertIsNone(desktop_locale.system_language())
        self.assertEqual(
            desktop_locale.resolved_language({"preference": "system", "system_language": "zh-TW"}), "zh-CN"
        )
        self.assertEqual(desktop_locale.resolved_language({"preference": "system", "system_language": None}), "en")

    def test_preference_is_available_before_login_and_survives_restart(self):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(desktop_locale, "system_language", return_value="en-US"),
        ):
            bridge = AppBridge(root)
            initial = {"preference": "system", "system_language": "en-US"}
            self.assertIsNone(bridge.identity)
            self.assertEqual(bridge.invoke("interface-language"), {"ok": True, "result": initial})
            updated = {"preference": "zh-CN", "system_language": "en-US"}
            self.assertEqual(
                bridge.invoke("set-interface-language", {"preference": "zh-CN"}), {"ok": True, "result": updated}
            )
            restarted = AppBridge(root)
            self.assertEqual(restarted.invoke("interface-language"), {"ok": True, "result": updated})
            for payload in ({}, {"preference": "fr"}, {"preference": ["en"]}):
                with self.subTest(payload=payload), self.assertLogs("agent_harness", level="ERROR"):
                    self.assertFalse(restarted.invoke("set-interface-language", payload)["ok"])
            self.assertEqual(restarted.invoke("interface-language"), {"ok": True, "result": updated})
            self.assertEqual(
                restarted.invoke("set-interface-language", {"preference": "system"}), {"ok": True, "result": initial}
            )
            (Path(root) / "interface-language.json").write_text("corrupt", encoding="utf-8")
            self.assertEqual(restarted.invoke("interface-language"), {"ok": True, "result": initial})

    def test_tray_text_reflects_language_switch(self):
        language = "en"
        icon = Mock(HAS_NOTIFICATION=True)
        make_icon = Mock(return_value=icon)
        pystray = SimpleNamespace(Icon=make_icon, Menu=lambda *items: items, MenuItem=lambda label, action, **kw: label)
        with patch.dict(sys.modules, {"pystray": pystray}), patch("agent_harness.tray._image", return_value=object()):
            tray = Tray("Foolship", lambda: None, lambda: None, language=lambda: language)
        tray._initialize_icon(icon)
        menu_items = make_icon.call_args.kwargs["menu"]
        status = TrayStatus(
            tray, lambda: None, lambda: (True, [{"running": True}]), threading.Event(), lambda: language
        )
        self.assertEqual([label(None) for label in menu_items], ["Show window", "Quit Foolship"])
        self.assertEqual(status.title(), "Local hub running · Receivers 1/1 running · Account locked")
        self.assertTrue(tray.notify("approval"))
        icon.notify.assert_called_once_with("New approval request. Open Foolship to review it.", "Foolship")
        language = "zh-CN"
        tray.refresh_menu()
        icon.update_menu.assert_called_once()
        self.assertEqual([label(None) for label in menu_items], ["显示窗口", "完全退出"])
        self.assertEqual(status.title(), "本机 Hub 运行中 · 接收端 1/1 运行 · 账户未解锁")
