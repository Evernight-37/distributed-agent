import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
            runtime = MagicMock()
            runtime.status.return_value = {"running": True}
            with (
                patch.object(linux_receiver.AppBridge, "_sync_proxy") as sync,
                patch.object(linux_receiver, "DesktopRuntime", return_value=runtime),
            ):
                linux_receiver._set_codex_proxy(root, {"enabled": True, "port": 7897})
            sync.assert_called_once()
            runtime._control.assert_called_once_with("action", {"action": "retire-workers-after-turn"})
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
            ["codex", "path", "set", "/opt/codex"],
            ["codex", "path", "detect"],
            ["codex", "path", "status"],
            ["service", "status"],
            ["service", "logs", "--lines", "20"],
            ["network", "broker-proxy", "set", "http://127.0.0.1:1"],
            ["network", "codex-proxy", "set", "--port", "7897"],
            ["doctor"],
            ["upgrade", "--bundle", "/tmp/release.tar.gz", "--sha256", "0" * 64, "--retry"],
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

    def test_codex_path_set_detect_and_status_sync_receivers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            receiver = root / "receivers/device-one"
            write_json(receiver / "pull-config.json", {})
            executable = root / "codex"
            executable.write_text("cli", encoding="utf-8")
            bridge = MagicMock()
            with (
                patch.object(linux_receiver, "codex_version", return_value=(1, 2, 3)),
                patch.object(linux_receiver, "AppBridge", return_value=bridge),
            ):
                result = linux_receiver.codex_path_command(root, "set", str(executable))
                status = linux_receiver.codex_path_command(root, "status")
            self.assertEqual(result["path"], str(executable.resolve()))
            self.assertEqual(status["version"], "1.2.3")
            bridge._sync_cli.assert_called_once()
            saved = read_json(root / "codex-cli.json")
            self.assertEqual(saved["mode"], "manual")
            self.assertNotIn("private_key", json.dumps(saved))

            detected = root / "detected-codex"
            detected.write_text("cli", encoding="utf-8")
            bridge.reset_mock()
            with (
                patch.object(
                    linux_receiver,
                    "cli_status",
                    return_value={"found": True, "configured": False, "path": str(detected), "version": "2.0.0"},
                ),
                patch.object(linux_receiver, "AppBridge", return_value=bridge),
            ):
                result = linux_receiver.codex_path_command(root, "detect")
            self.assertEqual(result["mode"], "auto")
            self.assertEqual(read_json(root / "codex-cli.json")["path"], str(detected))
            bridge._sync_cli.assert_called_once()

    def test_codex_path_rejects_relative_or_invalid_program(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "绝对路径"):
                linux_receiver.codex_path_command(folder, "set", "relative/codex")
            invalid = Path(folder) / "not-codex"
            invalid.write_text("bad", encoding="utf-8")
            with patch.object(linux_receiver, "codex_version", return_value=None):
                with self.assertRaisesRegex(ValueError, "无法运行"):
                    linux_receiver.codex_path_command(folder, "set", str(invalid))

    def test_codex_proxy_is_saved_when_worker_retirement_request_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            receiver = root / "receivers/device-one"
            write_json(receiver / "pull-config.json", {})
            runtime = MagicMock()
            runtime.status.return_value = {"running": True}
            runtime._control.side_effect = RuntimeError("old receiver")
            with (
                patch.object(linux_receiver.AppBridge, "_sync_proxy"),
                patch.object(linux_receiver, "DesktopRuntime", return_value=runtime),
            ):
                result = linux_receiver._set_codex_proxy(root, {"enabled": True, "port": 7897})
            self.assertEqual(read_json(root / "codex-proxy.json"), {"enabled": True, "port": 7897})
            self.assertEqual(result["warnings"][0]["receiver"], "device-one")
            self.assertIn("重启 Harness", result["note"])

    def test_root_execution_is_rejected_but_help_remains_available(self):
        with patch.object(os, "geteuid", return_value=0, create=True):
            with self.assertRaisesRegex(PermissionError, "root"):
                linux_receiver.main(["status"])
            with self.assertRaises(SystemExit) as help_exit:
                linux_receiver.main(["--help"])
            self.assertEqual(help_exit.exception.code, 0)

    def test_doctor_requires_0700_and_reports_project_disk_and_gpu(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.chmod(0o750)
            receiver = root / "receivers/device-one"
            project = root / "workspace"
            project.mkdir()
            write_json(receiver / "pull-config.json", {})
            write_json(receiver / "projects/local/project.json", {"remote_path": str(project)})
            fake_service = MagicMock()
            fake_service.status.return_value = {"active": False}
            fake_service.linger_status.return_value = {"available": True, "enabled": False}
            completed = MagicMock(returncode=0, stdout="NVIDIA Test, 24564\n", stderr="")
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
                patch.object(linux_receiver.shutil, "which", return_value="/usr/bin/nvidia-smi"),
                patch.object(linux_receiver.subprocess, "run", return_value=completed) as run,
            ):
                result = linux_receiver.doctor(root)
            self.assertFalse(result["private_root"])
            self.assertEqual(result["projects"][0]["path"], str(project.resolve()))
            self.assertTrue(result["projects"][0]["readable"])
            self.assertIn("free_bytes", result["root_disk"])
            self.assertEqual(result["gpu"]["devices"][0]["memory_total_mib"], 24564)
            self.assertIn("--query-gpu=name,memory.total", run.call_args.args[0])

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
            first, second = MagicMock(), MagicMock()
            with patch.object(mcp_adapter, "broker_opener", side_effect=[first, second]) as factory:
                backend = mcp_adapter.HarnessMcpBackend(grant, network)
                self.assertIs(backend._opener(), first)
                write_json(network, {"proxy": "http://127.0.0.1:18767"})
                self.assertIs(backend._opener(), second)
            self.assertEqual(
                [call.args[0] for call in factory.call_args_list],
                ["http://127.0.0.1:18766", "http://127.0.0.1:18767"],
            )

    def test_service_logs_include_bounded_receiver_files(self):
        manager = MagicMock()
        manager.logs.return_value = SimpleNamespace(returncode=0, stdout="journal\n", stderr="")
        manager.receiver_logs.return_value = [{"receiver": "device-one", "stderr": "receiver error"}]
        args = SimpleNamespace(service_action="logs", lines=20, follow=False)
        with (
            patch.object(linux_receiver, "_service_manager", return_value=manager),
            patch("builtins.print") as output,
        ):
            linux_receiver.service_command("/tmp/data", args)
        manager.logs.assert_called_once_with(20, False)
        manager.receiver_logs.assert_called_once_with(20)
        rendered = "\n".join(" ".join(map(str, call.args)) for call in output.call_args_list)
        self.assertIn("receiver error", rendered)


if __name__ == "__main__":
    unittest.main()
