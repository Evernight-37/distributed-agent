import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import runtime_paths


class DataHomeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.app = self.root / "app"
        self.platform = self.root / "platform"
        patches = [
            mock.patch.object(runtime_paths, "bundled_root", lambda: self.app),
            mock.patch.object(runtime_paths, "application_home", lambda: self.platform),
            # Treat every case as Windows unless a test says otherwise; macOS
            # deliberately keeps the platform location.
            mock.patch.object(runtime_paths, "sys", types.SimpleNamespace(platform="win32")),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.folder.cleanup)
        os.environ.pop("FOOLSHIP_STATE", None)

    def test_macos_keeps_the_platform_location(self):
        with mock.patch.object(runtime_paths, "sys", types.SimpleNamespace(platform="darwin")):
            self.app.mkdir(parents=True)
            self.assertEqual(runtime_paths.data_home(), self.platform / "data")

    def test_installation_files_live_in_the_application_directory(self):
        self.app.mkdir(parents=True)
        self.assertEqual(runtime_paths.installation_root(), self.app)

    def test_macos_never_writes_into_the_bundle(self):
        with mock.patch.object(runtime_paths, "sys", types.SimpleNamespace(platform="darwin")):
            self.assertEqual(runtime_paths.installation_root(), runtime_paths.application_home())

    def test_a_source_run_never_writes_into_the_checkout(self):
        with mock.patch.object(runtime_paths, "bundled_root", lambda: None):
            self.assertEqual(runtime_paths.installation_root(), runtime_paths.application_home())

    def test_an_explicit_override_wins(self):
        os.environ["FOOLSHIP_STATE"] = str(self.root / "custom")
        self.assertEqual(runtime_paths.data_home(), self.root / "custom")

    def test_a_fresh_copy_keeps_data_beside_the_application(self):
        self.app.mkdir(parents=True)
        self.assertEqual(runtime_paths.data_home(), self.app / "data")

    def test_an_existing_beside_directory_is_used(self):
        self.app.mkdir(parents=True)
        (self.app / "data").mkdir()
        self.platform.mkdir(parents=True)
        self.assertEqual(runtime_paths.data_home(), self.app / "data")

    def test_no_beside_directory_falls_back_to_the_platform_location(self):
        with mock.patch.object(runtime_paths, "bundled_root", lambda: None):
            self.assertEqual(runtime_paths.data_home(), self.platform / "data")

    def test_the_default_state_is_the_data_home(self):
        os.environ["FOOLSHIP_STATE"] = str(self.root / "custom")
        self.assertEqual(runtime_paths.default_state(), self.root / "custom")


if __name__ == "__main__":
    unittest.main()
