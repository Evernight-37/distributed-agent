import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.package_macos_release import package


class MacOSReleaseTests(unittest.TestCase):
    def test_package_requires_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "AgentHarness.app"
            app.mkdir()
            output = root / "AgentHarness.zip"
            with mock.patch("scripts.package_macos_release.subprocess.run") as run:
                result = package(app, output)
            self.assertEqual(result, output.resolve())
            self.assertTrue(output.parent.is_dir())
            run.assert_called_once()

    def test_package_rejects_non_app_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                package(Path(directory), Path(directory) / "out.zip")

    def test_release_requires_macos_job(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("needs: [windows, linux, macos]", workflow)
        self.assertIn("package_macos_release.py", workflow)
        self.assertNotIn("xcrun stapler", workflow)


if __name__ == "__main__":
    unittest.main()
