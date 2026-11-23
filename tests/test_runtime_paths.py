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

    def test_a_fresh_copy_uses_the_platform_location(self):
        self.app.mkdir(parents=True)
        self.assertEqual(runtime_paths.data_home(), self.platform / "data")

    def test_an_empty_beside_directory_is_not_used(self):
        self.app.mkdir(parents=True)
        (self.app / "data").mkdir()
        self.platform.mkdir(parents=True)
        self.assertEqual(runtime_paths.data_home(), self.platform / "data")

    def test_no_beside_directory_falls_back_to_the_platform_location(self):
        with mock.patch.object(runtime_paths, "bundled_root", lambda: None):
            self.assertEqual(runtime_paths.data_home(), self.platform / "data")

    def test_a_fresh_copy_finds_the_account_in_the_platform_directory(self):
        """Re-extracting the app elsewhere must not hide an existing account."""
        self.app.mkdir(parents=True)
        (self.platform / "data" / "profiles").mkdir(parents=True)
        (self.platform / "data" / "profiles" / "account.json").write_text("{}", encoding="utf-8")
        self.assertEqual(runtime_paths.data_home(), self.platform / "data")

    def test_an_older_beside_directory_is_migrated_once(self):
        """Data left beside the executable moves to the platform location."""
        self.app.mkdir(parents=True)
        (self.app / "data" / "profiles").mkdir(parents=True)
        (self.app / "data" / "profiles" / "account.json").write_text("{}", encoding="utf-8")
        (self.app / "data" / "team-service").mkdir(parents=True)
        (self.app / "data" / "team-service" / "marker").write_text("db", encoding="utf-8")
        self.assertEqual(runtime_paths.data_home(), self.platform / "data")
        self.assertTrue((self.platform / "data" / "profiles" / "account.json").is_file())
        self.assertTrue((self.platform / "data" / "team-service" / "marker").is_file())
        # The old directory is kept, renamed, never deleted.
        self.assertFalse((self.app / "data").exists())
        self.assertTrue(list(self.app.glob("data.migrated-*")))

    def test_a_failed_migration_keeps_working_from_the_old_location(self):
        import shutil

        self.app.mkdir(parents=True)
        (self.app / "data" / "profiles").mkdir(parents=True)
        (self.app / "data" / "profiles" / "account.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(shutil, "copytree", side_effect=OSError("disk full")):
            self.assertEqual(runtime_paths.data_home(), self.app / "data")
        self.assertTrue((self.app / "data" / "profiles" / "account.json").is_file())

    def test_the_platform_location_wins_when_both_exist(self):
        self.app.mkdir(parents=True)
        (self.app / "data" / "profiles").mkdir(parents=True)
        (self.app / "data" / "profiles" / "old.json").write_text("{}", encoding="utf-8")
        (self.platform / "data" / "profiles").mkdir(parents=True)
        (self.platform / "data" / "profiles" / "current.json").write_text("{}", encoding="utf-8")
        self.assertEqual(runtime_paths.data_home(), self.platform / "data")
        self.assertTrue((self.app / "data").exists(), "a second copy must not migrate again")

    def test_the_default_state_is_the_data_home(self):
        os.environ["FOOLSHIP_STATE"] = str(self.root / "custom")
        self.assertEqual(runtime_paths.default_state(), self.root / "custom")


if __name__ == "__main__":
    unittest.main()
