"""macOS paths and executable permissions must survive service installation."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness.desktop_runtime import web_assets
from agent_harness.runtime_paths import application_home
from agent_harness.service_install import (
    application_bundle,
    install_bundle,
    runtime_python,
)
from agent_harness import worker
from agent_harness.app_bridge import AppBridge


class PackagedWebAssetsTests(unittest.TestCase):
    def test_windows_and_macos_use_the_web_assets_from_the_verified_service(self):
        for platform, launcher in (
            ("win32", "Foolship.exe"),
            ("darwin", "Foolship.app/Contents/MacOS/Foolship"),
        ):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bundle = root / "runtime-copy"
                web = bundle / "client" / "agent_harness" / "web"
                web.mkdir(parents=True)
                (web / "app.html").write_text("current", encoding="utf-8")
                executable = root / launcher
                legacy = executable.parent / "_internal" / "web"
                legacy.mkdir(parents=True)
                (legacy / "app.html").write_text("stale", encoding="utf-8")
                with (
                    mock.patch.object(sys, "frozen", True, create=True),
                    mock.patch.object(sys, "platform", platform),
                    mock.patch.object(sys, "executable", str(executable)),
                    mock.patch("agent_harness.service_install.installed_bundle", return_value=bundle),
                ):
                    self.assertEqual(web_assets(), web)


@unittest.skipUnless(sys.platform == "darwin", "macOS packaging")
class MacPackagingTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX directory access permissions")
    def test_private_directory_can_still_create_and_lock_service_files(self):
        from agent_harness.common import lock

        with tempfile.TemporaryDirectory() as directory:
            bridge = AppBridge(Path(directory) / "data")
            state = bridge._host_state_root()
            state.mkdir()
            bridge._secure(state)
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            with lock(state / "desktop-host-start.lock"):
                self.assertTrue((state / "desktop-host-start.lock").is_file())
            private = state / "identity.json"
            private.write_text("{}")
            bridge._secure(private)
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)

    def test_application_data_lives_outside_bundle(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("sys.platform", "darwin"),
        ):
            self.assertEqual(
                application_home(),
                Path.home() / "Library/Application Support/Foolship",
            )

    def test_bundle_resources_are_found_from_native_launcher(self):
        with (
            mock.patch("sys.platform", "darwin"),
            mock.patch("sys.executable", "/tmp/Test.app/Contents/MacOS/Foolship"),
        ):
            self.assertEqual(
                application_bundle(),
                Path("/tmp/Test.app/Contents/Resources/service-bundle").resolve(),
            )

    def test_installed_runtime_keeps_execute_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            interpreter = bundle / "runtime/bin/python3"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("#!/bin/sh\nexit 0\n")
            interpreter.chmod(0o755)
            client = bundle / "client" / "agent_harness"
            client.mkdir(parents=True)
            (client / "desktop_service.py").write_text("# test service\n")
            installed = install_bundle(bundle, root / "installed")
            self.assertTrue(os.access(runtime_python(installed), os.X_OK))

    def test_finder_launch_discovers_bundled_cli_without_shell_path(self):
        expected = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("sys.platform", "darwin"),
            mock.patch("shutil.which", return_value=None),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(
                worker,
                "codex_version",
                side_effect=lambda path: (1, 2, 3) if str(path) == expected else None,
            ),
        ):
            result = worker.cli_status(directory)
            self.assertTrue(result["found"])
            self.assertEqual(result["path"], expected)
