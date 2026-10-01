import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_bridge import AppBridge
from common import read_json, write_json
from worker import cli_status


class CodexCliDiscoveryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows npm shims are .cmd files")
    def test_npm_cli_is_detected_even_when_background_path_is_stale(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shim = root / "roaming" / "npm" / "codex.cmd"
            shim.parent.mkdir(parents=True)
            shim.write_bytes(b"@echo off\r\necho codex-cli 0.300.1\r\n")
            env = {"APPDATA": str(root / "roaming"), "LOCALAPPDATA": str(root / "local"), "PATH": ""}
            with patch.dict(os.environ, env), patch("shutil.which", return_value=None):
                result = cli_status(root / "receiver")
            self.assertEqual(result, {"found": True, "configured": False,
                                      "path": str(shim), "version": "0.300.1"})

    def test_invalid_explicit_path_has_actionable_status(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"cli_path": str(root / "missing.exe")})
            result = cli_status(root)
            self.assertFalse(result["found"])
            self.assertIn("重新选择", result["error"])

    def test_app_caches_detected_cli_until_explicit_redetect(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / 'codex.exe'
            executable.write_bytes(b'')
            bridge = AppBridge(root)
            bridge.profile = {'node': 'device-local', 'server': 'http://127.0.0.1:8765'}
            bridge.identity = {'account_id': 'local'}
            detected = {'found': True, 'configured': False, 'path': str(executable), 'version': '0.300.1'}
            with patch('worker.cli_status', return_value=detected) as probe:
                self.assertTrue(bridge.invoke('codex-cli-status')['result']['found'])
                self.assertTrue(bridge.invoke('codex-cli-status')['result']['found'])
                self.assertEqual(probe.call_count, 1)
                self.assertEqual(read_json(root / 'codex-cli.json')['path'], str(executable))
                self.assertTrue(bridge.invoke('codex-cli-redetect')['result']['found'])
                self.assertEqual(probe.call_count, 2)

    @unittest.skipUnless(os.name == "nt", "Windows npm shims are .cmd files")
    def test_path_cli_is_detected_outside_standard_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shim = root / "custom" / "codex.cmd"
            shim.parent.mkdir()
            shim.write_bytes(b"@echo off\r\necho codex-cli 0.301.0\r\n")
            env = {"APPDATA": str(root / "empty-roaming"), "LOCALAPPDATA": str(root / "empty-local")}
            with patch.dict(os.environ, env), patch("shutil.which", return_value=str(shim)):
                result = cli_status(root / "receiver")
            self.assertTrue(result["found"])
            self.assertEqual(result["path"], str(shim))

    @unittest.skipUnless(os.name == "nt", "Windows npm shims are .cmd files")
    def test_app_can_save_and_clear_local_cli_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shim = root / "codex.cmd"
            shim.write_bytes(b"@echo off\r\necho codex-cli 0.300.1\r\n")
            receiver = root / "receivers" / "device-local"
            receiver.mkdir(parents=True)
            bridge = AppBridge(root)
            bridge.profile = {"node": "device-local", "server": "http://127.0.0.1:8765"}
            bridge.identity = {"account_id": "local"}
            saved = bridge.invoke("codex-cli-save", {"path": str(shim)})
            self.assertTrue(saved["ok"])
            self.assertTrue(saved["result"]["found"])
            self.assertEqual(read_json(receiver / "executor.json")["cli_path"], str(shim))
            self.assertEqual(read_json(root / "codex-cli.json")["path"], str(shim))
            self.assertTrue(bridge.invoke("codex-cli-status")["result"]["found"])
            cleared = bridge.invoke("codex-cli-save", {"path": ""})
            self.assertTrue(cleared["ok"])
            self.assertEqual(read_json(root / "codex-cli.json")["mode"], "auto")
            if cleared["result"]["found"]:
                self.assertEqual(read_json(receiver / "executor.json")["cli_path"], cleared["result"]["path"])


if __name__ == "__main__":
    unittest.main()
