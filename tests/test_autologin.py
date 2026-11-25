import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import autologin
from agent_harness.app_bridge import AppBridge
from agent_harness.common import write_json


class AutoLoginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

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

    def test_startup_notice_is_consumed_by_local_status(self):
        bridge = AppBridge(self.root)
        self.assertFalse(bridge.auto_login())
        self.assertFalse(bridge.invoke("local-status")["result"]["autologin_failed"])

        for invalid in ("{invalid", "null"):
            with self.subTest(invalid=invalid):
                remembered = self.root / autologin.FILE
                remembered.write_text(invalid, encoding="utf-8")
                self.assertFalse(bridge.auto_login())
                first = bridge.invoke("local-status")["result"]
                self.assertFalse(first["unlocked"])
                self.assertTrue(first["autologin_failed"])
                self.assertFalse(remembered.exists())
                self.assertFalse(bridge.invoke("local-status")["result"]["autologin_failed"])

    def test_rejected_saved_unlock_discards_partial_login(self):
        bridge = AppBridge(self.root)
        write_json(self.root / "profiles" / "profile-1.json", {"id": "profile-1", "username": "Test account"})
        write_json(self.root / autologin.FILE, {"profile_id": "profile-1", "envelope": {"backend": "test"}})
        dispatch = bridge.control_api.invoke

        def failed_login(action, data):
            if action == "login":
                bridge.identity = {"id": "private identity"}
                bridge.profile = {"id": "profile-1"}
                raise RuntimeError("private diagnostic")
            return dispatch(action, data)

        with (
            mock.patch.object(autologin.os_secrets, "unprotect", return_value=b"secret"),
            mock.patch.object(bridge.control_api, "invoke", side_effect=failed_login),
            self.assertLogs("agent_harness", level="ERROR"),
        ):
            self.assertFalse(bridge.auto_login())
        status = bridge.invoke("local-status")["result"]
        self.assertTrue(status["autologin_failed"])
        self.assertFalse(status["unlocked"])
        self.assertIsNone(bridge.profile)

    def test_successful_saved_unlock_reads_the_normal_bridge_response(self):
        bridge = AppBridge(self.root)
        write_json(self.root / "profiles" / "profile-1.json", {"id": "profile-1", "username": "Test account"})
        write_json(self.root / autologin.FILE, {"profile_id": "profile-1", "envelope": {"backend": "test"}})
        with (
            mock.patch.object(autologin.os_secrets, "unprotect", return_value=b"secret"),
            mock.patch.object(bridge.control_api, "invoke", return_value={"unlocked": True}) as action,
        ):
            self.assertTrue(bridge.auto_login())
        action.assert_called_once_with("login", {"profile": "profile-1", "password": "secret", "remember": True})
        self.assertFalse(bridge.invoke("local-status")["result"]["autologin_failed"])


if __name__ == "__main__":
    unittest.main()
