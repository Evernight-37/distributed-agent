import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.app_bridge import AppBridge
from agent_harness.common import (
    apply_local_proxy,
    read_json,
    validate_local_proxy,
    write_json,
)
from agent_harness.worker import codex_login_status, executor_environment
from agent_harness import warm_worker


class ProxySettingsTests(unittest.TestCase):
    def test_port_validation_and_child_environment(self):
        for port in (0, -1, 65536, True, "7897", 1.5):
            with self.subTest(port=port), self.assertRaises(ValueError):
                validate_local_proxy({"enabled": True, "port": port})
        with self.assertRaises(ValueError):
            validate_local_proxy({"enabled": True, "port": 7897, "host": "example.com"})

        original = {
            "HTTPS_PROXY": "http://old:1",
            "all_proxy": "http://old:2",
            "OTHER": "keep",
        }
        enabled = apply_local_proxy(original, {"enabled": True, "port": 7897})
        self.assertEqual(enabled["HTTPS_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(enabled["HTTP_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(enabled["OTHER"], "keep")
        self.assertNotIn("all_proxy", enabled)
        self.assertEqual(original["HTTPS_PROXY"], "http://old:1")
        disabled = apply_local_proxy(enabled, {"enabled": False, "port": 7897})
        self.assertEqual(disabled, {"OTHER": "keep"})

    def test_login_preflight_uses_same_saved_proxy_as_codex_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"proxy": {"enabled": True, "port": 7897}})
            env = executor_environment(root)
            self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7897")
            self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7897")
            with patch.object(warm_worker, "ROOT", root):
                self.assertEqual(warm_worker.local_env()["HTTPS_PROXY"], "http://127.0.0.1:7897")
            with patch("agent_harness.worker.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "Logged in using ChatGPT"
                run.return_value.stderr = ""
                self.assertEqual(codex_login_status("codex.exe", root), {"logged_in": True})
                self.assertEqual(run.call_args.kwargs["env"]["HTTPS_PROXY"], "http://127.0.0.1:7897")
                run.return_value.returncode = 1
                run.return_value.stdout = "Not logged in"
                status = codex_login_status("codex.exe", root)
                self.assertFalse(status["logged_in"])
                self.assertIn("尚未登录", status["login_error"])

    def test_app_setting_updates_local_receivers_without_touching_cli_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            receiver = root / "receivers" / "device-local"
            write_json(
                receiver / "executor.json",
                {
                    "cli_path": "C:/Codex/codex.exe",
                    "network_env": {
                        "HTTP_PROXY": "http://127.0.0.1:7897",
                        "HTTPS_PROXY": "http://127.0.0.1:7897",
                        "OTHER": "keep",
                    },
                },
            )
            bridge = AppBridge(root)
            bridge.profile = {"node": "device-local", "server": "http://127.0.0.1:8765"}
            bridge.identity = {"account_id": "local"}
            self.assertEqual(bridge._proxy_settings(), {"enabled": True, "port": 7897})
            self.assertEqual(
                bridge.invoke("proxy-save", {"enabled": True, "port": 7897}),
                {"ok": True, "result": {"enabled": True, "port": 7897}},
            )
            executor = read_json(receiver / "executor.json")
            self.assertEqual(executor["proxy"], {"enabled": True, "port": 7897})
            self.assertEqual(executor["cli_path"], "C:/Codex/codex.exe")
            self.assertEqual(executor["network_env"], {"OTHER": "keep"})
            self.assertEqual(read_json(root / "codex-proxy.json"), executor["proxy"])
            self.assertEqual(
                bridge.invoke("proxy-save", {"enabled": False, "port": None})["ok"],
                True,
            )
            self.assertEqual(
                read_json(receiver / "executor.json")["proxy"],
                {"enabled": False, "port": None},
            )
            self.assertFalse(bridge.invoke("proxy-save", {"enabled": True, "port": 0})["ok"])


if __name__ == "__main__":
    unittest.main()
