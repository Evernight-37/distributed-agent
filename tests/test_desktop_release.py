"""Native window fallback and deterministic release metadata."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.build_desktop import (
    APP_SOURCE,
    CLIENT_FILES,
    application_version,
    getting_started_text,
    write_release_metadata,
)
from agent_harness.production_app import request_close_dialog


class NativeCloseTests(unittest.TestCase):
    def test_loaded_page_handles_close_with_its_choice_dialog(self):
        window, bridge = mock.Mock(), mock.Mock()
        bridge.allow_window_close = False
        window.evaluate_js.return_value = True
        self.assertTrue(request_close_dialog(window, bridge))
        self.assertFalse(bridge.allow_window_close)
        window.destroy.assert_not_called()

    def test_missing_javascript_closes_window_and_keeps_services(self):
        window, bridge = mock.Mock(), mock.Mock()
        bridge.allow_window_close = False
        window.evaluate_js.return_value = False
        self.assertFalse(request_close_dialog(window, bridge))
        self.assertTrue(bridge.allow_window_close)
        window.destroy.assert_called_once()

    def test_evaluate_failure_has_same_safe_background_fallback(self):
        window, bridge = mock.Mock(), mock.Mock()
        bridge.allow_window_close = False
        window.evaluate_js.side_effect = RuntimeError("webview unavailable")
        self.assertFalse(request_close_dialog(window, bridge))
        self.assertTrue(bridge.allow_window_close)
        window.destroy.assert_called_once()


class ReleaseMetadataTests(unittest.TestCase):
    def test_service_bundle_includes_every_production_module(self):
        expected = {path.name for path in APP_SOURCE.glob("*.py")} - {"__init__.py"}
        self.assertEqual(set(CLIENT_FILES), expected)
        self.assertIn("health.py", CLIENT_FILES)
        self.assertIn("account_recovery.py", CLIENT_FILES)

    def test_version_manifest_and_guide_are_driven_by_version_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            (source / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            self.assertEqual(application_version(source), "9.8.7")
            self.assertEqual(write_release_metadata(output, source), "9.8.7")
            self.assertEqual((output / "VERSION").read_text(encoding="utf-8"), "9.8.7\n")
            manifest = json.loads((output / "app-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["name"], "AgentHarness")
            self.assertEqual(manifest["version"], "9.8.7")
            self.assertRegex(manifest["python_runtime"], r"^3\.14\.")
            guide = getting_started_text(application_version(source))
            self.assertTrue(guide.startswith("Agent Harness 9.8.7\n"))
            self.assertIn(r"%LOCALAPPDATA%\AgentHarness\data", guide)
            self.assertIn("team-service", guide)
            self.assertIn("升级或移动应用目录不会改变", guide)
            self.assertIn("在 Codex 中使用协作", guide)
            self.assertIn("受限身份", guide)

    def test_invalid_version_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "VERSION").write_text("6.1 beta", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "VERSION"):
                application_version(source)


if __name__ == "__main__":
    unittest.main()
