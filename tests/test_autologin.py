import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import autologin


class AutoLoginTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_remembering_is_off_without_os_protection(self):
        with mock.patch.object(autologin.os_secrets, "protect", return_value=None):
            self.assertFalse(autologin.save(self.root, "profile-1", "pw"))
        self.assertIsNone(autologin.status(self.root))

    def test_one_account_round_trips_and_never_stores_the_password(self):
        envelope = {"backend": "test", "payload": "x"}
        with mock.patch.object(autologin.os_secrets, "protect", return_value=envelope):
            self.assertTrue(autologin.save(self.root, "profile-1", "s3cret"))
        raw = (self.root / autologin.FILE).read_text(encoding="utf-8")
        self.assertNotIn("s3cret", raw)
        self.assertEqual(autologin.status(self.root), "profile-1")
        with mock.patch.object(autologin.os_secrets, "unprotect", return_value=b"s3cret"):
            self.assertEqual(autologin.load(self.root), ("profile-1", "s3cret"))

    def test_an_unreadable_secret_is_forgotten(self):
        with mock.patch.object(autologin.os_secrets, "protect", return_value={"backend": "test"}):
            autologin.save(self.root, "profile-1", "s3cret")
        with mock.patch.object(autologin.os_secrets, "unprotect", side_effect=PermissionError("no")):
            self.assertIsNone(autologin.load(self.root))
        self.assertIsNone(autologin.status(self.root), "a broken entry must not linger")

    def test_clearing_removes_the_file(self):
        with mock.patch.object(autologin.os_secrets, "protect", return_value={"backend": "test"}):
            autologin.save(self.root, "profile-1", "pw")
        autologin.clear(self.root)
        self.assertIsNone(autologin.load(self.root))


if __name__ == "__main__":
    unittest.main()
