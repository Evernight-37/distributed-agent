import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import linux_receiver
import mcp_adapter
from common import read_json, write_json


class LinuxReceiverTests(unittest.TestCase):
    def test_stable_mcp_launcher_is_private_and_upgradeable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = linux_receiver._write_launcher(root, "profile-one")
            content = first.read_text(encoding="utf-8")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o700)
            self.assertTrue(content.startswith("#!/bin/sh\nexec "))
            self.assertIn("--grant", content)
            self.assertIn("--network", content)
            second = linux_receiver._write_launcher(root, "profile-one")
            self.assertEqual(first, second)
            self.assertEqual(first.read_text(encoding="utf-8"), content)

    def test_unlock_uses_empty_password_without_prompt(self):
        bridge = MagicMock()
        bridge._profiles.return_value = [{"id": "profile-one"}]
        with (
            patch.object(linux_receiver, "AppBridge", return_value=bridge),
            patch.object(linux_receiver, "invoke", return_value={"ok": True, "result": {}}) as invoke,
            patch.object(linux_receiver, "getpass") as getpass,
        ):
            self.assertIs(linux_receiver.unlock_bridge("/tmp/data"), bridge)
            invoke.assert_called_once_with(bridge, "login", {"profile": "profile-one", "password": ""})
            getpass.getpass.assert_not_called()

    def test_unlock_prompts_only_after_local_unlock_failure(self):
        bridge = MagicMock()
        bridge._profiles.return_value = [{"id": "profile-one"}]
        calls = [RuntimeError("Unable to unlock local account"), {"ok": True, "result": {}}]

        def invoke_side_effect(*args):
            value = calls.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with (
            patch.object(linux_receiver, "AppBridge", return_value=bridge),
            patch.object(linux_receiver, "invoke", side_effect=invoke_side_effect),
            patch.object(linux_receiver, "getpass", return_value="not printed") as reader,
        ):
            self.assertIs(linux_receiver.unlock_bridge("/tmp/data", reader), bridge)
            reader.assert_called_once()

    def test_proxy_updates_preserve_receiver_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            receiver = root / "receivers" / "device-one"
            write_json(receiver / "pull-config.json", {"server": "https://team", "device_token": "keep"})
            write_json(receiver / "executor.json", {"cli_path": "/opt/codex", "warm_idle_seconds": 120})
            with patch.object(linux_receiver, "broker_opener") as opener:
                linux_receiver._set_broker_proxy(root, "http://127.0.0.1:18766")
            opener.assert_called_once_with("http://127.0.0.1:18766")
            self.assertEqual(read_json(receiver / "pull-config.json")["device_token"], "keep")
            self.assertEqual(read_json(receiver / "pull-config.json")["broker_proxy"], "http://127.0.0.1:18766")
            with patch.object(linux_receiver.AppBridge, "_sync_proxy") as sync:
                linux_receiver._set_codex_proxy(root, {"enabled": True, "port": 7897})
            sync.assert_called_once()
            self.assertEqual(read_json(receiver / "executor.json")["cli_path"], "/opt/codex")
            self.assertEqual(read_json(root / "codex-proxy.json"), {"enabled": True, "port": 7897})
            linux_receiver._set_broker_proxy(root, None)
            self.assertNotIn("broker_proxy", read_json(receiver / "pull-config.json"))
            self.assertFalse((root / "broker-network.json").exists())

    def test_status_reports_each_receiver(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("device-one", "device-two"):
                write_json(root / "receivers" / name / "pull-config.json", {})
            fake_runtime = MagicMock()
            fake_runtime.status.side_effect = [{"running": True}, {"running": False}]
            with (
                patch.object(linux_receiver, "DesktopRuntime", return_value=fake_runtime),
                patch.object(linux_receiver, "cli_status", side_effect=[{"found": False}, {"found": False}]) as cli,
                patch.object(linux_receiver, "invoke", return_value={"profiles": []}),
            ):
                result = linux_receiver.status(root)
            self.assertEqual([item["receiver"] for item in result["receivers"]], ["device-one", "device-two"])
            self.assertEqual(cli.call_count, 2)

    def test_parser_exposes_management_commands(self):
        parser = linux_receiver.build_parser()
        for argv in (
            ["codex", "connect"],
            ["service", "status"],
            ["service", "logs", "--lines", "20"],
            ["network", "broker-proxy", "set", "http://127.0.0.1:1"],
            ["network", "codex-proxy", "set", "--port", "7897"],
            ["doctor"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).command, argv[0])

    def test_doctor_output_does_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.chmod(0o700)
            fake_service = MagicMock()
            fake_service.status.return_value = {"active": False}
            fake_service.linger_status.return_value = {"available": False, "enabled": None}
            with (
                patch.object(linux_receiver, "status", return_value={"profiles": [], "receivers": []}),
                patch.object(
                    linux_receiver,
                    "network_status",
                    return_value={
                        "broker_proxy": {"enabled": False, "address": None},
                        "codex_proxy": {"enabled": False, "port": None},
                    },
                ),
                patch.object(linux_receiver, "ServiceManager", return_value=fake_service),
            ):
                result = linux_receiver.doctor(root)
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("private_key", serialized)
            self.assertNotIn("device_token", serialized)

    def test_mcp_adapter_uses_explicit_network_proxy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            grant = root / "grant.json"
            write_json(
                grant,
                {
                    "version": 1,
                    "identity": {"private_key": "local", "public_key": "public", "account_id": "account"},
                    "profile_id": "profile",
                    "username": "owner",
                    "servers": ["https://team.example"],
                    "scopes": ["collaboration.read", "collaboration.message", "collaboration.task"],
                },
            )
            network = root / "network.json"
            write_json(network, {"proxy": "http://127.0.0.1:18766"})
            opener = MagicMock()
            with patch.object(mcp_adapter, "broker_opener", return_value=opener) as factory:
                backend = mcp_adapter.HarnessMcpBackend(grant, network)
            factory.assert_called_once_with("http://127.0.0.1:18766")
            self.assertIs(backend.opener, opener)


if __name__ == "__main__":
    unittest.main()
