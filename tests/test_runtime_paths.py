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
        self.legacy = self.root / "legacy" / "data"
        self.app = self.root / "app"
        patches = [
            mock.patch.object(runtime_paths, "legacy_data", lambda: self.legacy),
            mock.patch.object(runtime_paths, "bundled_root", lambda: self.app),
            # Treat every case as Windows unless a test says otherwise; macOS
            # deliberately keeps the platform location.
            mock.patch.object(runtime_paths, "sys", types.SimpleNamespace(platform="win32")),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.folder.cleanup)
        os.environ.pop("AGENT_HARNESS_STATE", None)

    def test_macos_keeps_the_platform_location(self):
        with mock.patch.object(runtime_paths, "sys", types.SimpleNamespace(platform="darwin")):
            self.app.mkdir(parents=True)
            self.assertEqual(runtime_paths.data_home(), self.legacy)

    def test_an_explicit_override_wins(self):
        os.environ["AGENT_HARNESS_STATE"] = str(self.root / "custom")
        self.assertEqual(runtime_paths.data_home(), self.root / "custom")

    def test_a_fresh_copy_keeps_data_beside_the_application(self):
        self.app.mkdir(parents=True)
        self.assertEqual(runtime_paths.data_home(), self.app / "data")

    def test_an_existing_beside_directory_is_used(self):
        self.app.mkdir(parents=True)
        (self.app / "data").mkdir()
        self.legacy.mkdir(parents=True)
        self.assertEqual(runtime_paths.data_home(), self.app / "data")

    def test_an_older_install_keeps_its_data_until_it_is_migrated(self):
        self.app.mkdir(parents=True)
        self.legacy.mkdir(parents=True)
        (self.legacy / "desktop.db").write_text("old", encoding="utf-8")
        self.assertEqual(runtime_paths.data_home(), self.legacy)

    def test_no_beside_directory_falls_back_to_the_platform_location(self):
        with mock.patch.object(runtime_paths, "bundled_root", lambda: None):
            self.assertEqual(runtime_paths.data_home(), self.legacy)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.legacy = self.root / "legacy" / "data"
        self.legacy.mkdir(parents=True)
        (self.legacy / "desktop.db").write_text("rows", encoding="utf-8")
        (self.legacy / "keys").mkdir()
        (self.legacy / "keys" / "identity.bin").write_bytes(b"secret")
        self.destination = self.root / "app" / "data"

    def test_migration_copies_then_keeps_the_original_as_a_backup(self):
        result = runtime_paths.migrate_data(self.destination, self.legacy)
        self.assertEqual(result, self.destination)
        self.assertEqual((self.destination / "desktop.db").read_text(encoding="utf-8"), "rows")
        self.assertEqual((self.destination / "keys" / "identity.bin").read_bytes(), b"secret")
        self.assertFalse(self.legacy.exists())
        backups = list(self.legacy.parent.glob("data.migrated-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "desktop.db").is_file())
        self.assertFalse(list(self.destination.parent.glob("data.migrating")))

    def test_a_failed_copy_leaves_the_original_in_use(self):
        target = mock.patch.object(runtime_paths.shutil, "copytree", side_effect=OSError("disk full"))
        with target:
            result = runtime_paths.migrate_data(self.destination, self.legacy)
        self.assertEqual(result, self.legacy)
        self.assertFalse(self.destination.exists())
        self.assertTrue((self.legacy / "desktop.db").is_file())

    def test_migration_is_skipped_when_data_already_exists(self):
        self.destination.mkdir(parents=True)
        self.assertEqual(runtime_paths.migrate_data(self.destination, self.legacy), self.destination)
        self.assertTrue((self.legacy / "desktop.db").is_file())

    def test_nothing_happens_without_legacy_data(self):
        legacy = self.root / "missing"
        self.assertEqual(runtime_paths.migrate_data(self.destination, legacy), self.destination)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
