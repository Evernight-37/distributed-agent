import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.package_windows_release import package_release


class PackageWindowsReleaseTests(unittest.TestCase):
    def test_archive_has_stable_root_and_unicode_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release" / "AgentHarness"
            source.mkdir(parents=True)
            for name, content in {
                "AgentHarness.exe": b"app",
                "VERSION": b"1.2.0\n",
                "app-manifest.json": b"{}",
                "\u5f00\u59cb\u4f7f\u7528.txt": "\u8bf4\u660e".encode(),
            }.items():
                (source / name).write_bytes(content)
            destination = root / "AgentHarness-1.2.0-windows-x64.zip"

            package_release(source, destination)

            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(
                    archive.namelist(),
                    [
                        "AgentHarness/AgentHarness.exe",
                        "AgentHarness/app-manifest.json",
                        "AgentHarness/VERSION",
                        "AgentHarness/\u5f00\u59cb\u4f7f\u7528.txt",
                    ],
                )
                self.assertEqual(
                    archive.read("AgentHarness/\u5f00\u59cb\u4f7f\u7528.txt"),
                    "\u8bf4\u660e".encode(),
                )

    def test_rejects_incomplete_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "AgentHarness"
            source.mkdir()
            with self.assertRaisesRegex(ValueError, "missing AgentHarness.exe"):
                package_release(source, root / "release.zip")


if __name__ == "__main__":
    unittest.main()
