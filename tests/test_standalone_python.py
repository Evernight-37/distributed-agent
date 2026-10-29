"""Prevent release runtimes from depending on the builder's system Python."""

import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.prepare_python_runtime import artifact, prepare, validate_runtime, verify_archive


class StandalonePythonTests(unittest.TestCase):
    def test_framework_python_is_rejected_even_when_probe_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
            python.parent.mkdir()
            python.touch()
            probe = {"version": list(sys.version_info[:2]), "framework": "Python"}
            with patch(
                "scripts.prepare_python_runtime.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(probe))
            ):
                with self.assertRaisesRegex(RuntimeError, "Framework Python is not relocatable"):
                    validate_runtime(root)

    def test_builder_cannot_mix_a_different_python_distribution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
            python.parent.mkdir()
            python.touch()
            probe = {"version": list(sys.version_info[:2]), "framework": ""}
            with patch(
                "scripts.prepare_python_runtime.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(probe))
            ):
                with self.assertRaisesRegex(RuntimeError, "venv created by the selected"):
                    validate_runtime(root)

    def test_platform_aliases_select_same_pinned_artifact(self):
        self.assertEqual(artifact("Linux", "aarch64"), artifact("Linux", "arm64"))
        self.assertEqual(artifact("Linux", "amd64"), artifact("Linux", "x86_64"))
        self.assertNotEqual(artifact("Darwin", "arm64"), artifact("Linux", "arm64"))

    def test_corrupt_download_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "python.tar.gz"
            archive.write_bytes(b"not a Python distribution")
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                verify_archive(archive, "0" * 64)

    def test_archive_path_traversal_cannot_escape_staging(self):
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode="w:gz") as stream:
            entry = tarfile.TarInfo("../../outside")
            entry.size = 1
            stream.addfile(entry, io.BytesIO(b"x"))
        payload = content.getvalue()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "scripts.prepare_python_runtime.artifact",
                return_value=("python.tar.gz", hashlib.sha256(payload).hexdigest()),
            ),
            patch("scripts.prepare_python_runtime.urllib.request.urlopen", return_value=io.BytesIO(payload)),
        ):
            root = Path(directory)
            with self.assertRaises(tarfile.FilterError):
                prepare(root / "runtime")
            self.assertFalse((root / "outside").exists())
            self.assertFalse((root / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
