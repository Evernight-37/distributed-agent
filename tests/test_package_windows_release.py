import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.package_windows_release import package_release


class PackageWindowsReleaseTests(unittest.TestCase):
    def test_archive_has_stable_root_and_unicode_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release" / "Foolship"
            source.mkdir(parents=True)
            for name, content in {
                "Foolship.exe": b"app",
                "VERSION": b"1.2.0\n",
                "app-manifest.json": b"{}",
                "\u5f00\u59cb\u4f7f\u7528.txt": "\u8bf4\u660e".encode(),
            }.items():
                (source / name).write_bytes(content)
            destination = root / "Foolship-1.2.0-windows-x64.zip"

            package_release(source, destination)

            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(
                    archive.namelist(),
                    [
                        "Foolship/app-manifest.json",
                        "Foolship/Foolship.exe",
                        "Foolship/VERSION",
                        "Foolship/\u5f00\u59cb\u4f7f\u7528.txt",
                    ],
                )
                self.assertEqual(
                    archive.read("Foolship/\u5f00\u59cb\u4f7f\u7528.txt"),
                    "\u8bf4\u660e".encode(),
                )

    def test_rejects_incomplete_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Foolship"
            source.mkdir()
            with self.assertRaisesRegex(ValueError, "missing Foolship.exe"):
                package_release(source, root / "release.zip")


if __name__ == "__main__":
    unittest.main()
