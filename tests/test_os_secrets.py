import base64
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from agent_harness import os_secrets


class PlatformSecretBackendTests(unittest.TestCase):
    def test_macos_keychain_round_trip_commands(self):
        stored = {}

        def add(reference, value):
            stored[reference] = value

        def get(reference):
            return stored[reference]

        with (
            mock.patch.object(os_secrets, "os", SimpleNamespace(name="posix")),
            mock.patch.object(os_secrets.sys, "platform", "darwin"),
            mock.patch.object(os_secrets, "_mac_keychain_add", side_effect=add) as added,
            mock.patch.object(os_secrets, "_mac_keychain_get", side_effect=get) as fetched,
        ):
            envelope = os_secrets.protect(b"encrypted-profile")
            self.assertEqual(envelope["backend"], "macos-keychain")
            self.assertNotIn("payload", envelope)
            self.assertEqual(os_secrets.unprotect(envelope), b"encrypted-profile")
        self.assertEqual(added.call_count, 1)
        self.assertEqual(fetched.call_count, 1)

    def test_linux_secret_service_round_trip_commands(self):
        encoded = base64.b64encode(b"encrypted-profile").decode("ascii")
        results = [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout=encoded + "\n", stderr=""),
        ]
        with (
            mock.patch.object(os_secrets, "os", SimpleNamespace(name="posix")),
            mock.patch.object(os_secrets.sys, "platform", "linux"),
            mock.patch.object(os_secrets.shutil, "which", return_value="/usr/bin/secret-tool"),
            mock.patch.object(os_secrets.subprocess, "run", side_effect=results) as invoked,
        ):
            envelope = os_secrets.protect(b"encrypted-profile")
            self.assertEqual(envelope["backend"], "linux-secret-service")
            self.assertEqual(os_secrets.unprotect(envelope), b"encrypted-profile")
        self.assertIn("store", invoked.call_args_list[0].args[0])
        self.assertEqual(invoked.call_args_list[0].kwargs["input"], encoded)
        self.assertIn("lookup", invoked.call_args_list[1].args[0])

    def test_platform_backend_unavailable_uses_encrypted_file_fallback(self):
        with (
            mock.patch.object(os_secrets, "os", SimpleNamespace(name="posix")),
            mock.patch.object(os_secrets.sys, "platform", "linux"),
            mock.patch.object(os_secrets.shutil, "which", return_value=None),
        ):
            self.assertIsNone(os_secrets.protect(b"encrypted-profile"))

    def test_secret_service_failure_uses_encrypted_file_fallback(self):
        with (
            mock.patch.object(os_secrets, "os", SimpleNamespace(name="posix")),
            mock.patch.object(os_secrets.sys, "platform", "linux"),
            mock.patch.object(os_secrets.shutil, "which", return_value="secret-tool"),
            mock.patch.object(
                os_secrets.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("secret-tool", 10),
            ),
        ):
            self.assertIsNone(os_secrets.protect(b"encrypted-profile"))

    def test_cross_platform_envelope_is_rejected_without_lookup(self):
        with (
            mock.patch.object(os_secrets.sys, "platform", "linux"),
            mock.patch.object(os_secrets.subprocess, "run") as invoked,
        ):
            with self.assertRaises(PermissionError):
                os_secrets.unprotect({"backend": "macos-keychain", "reference": "a" * 32})
        invoked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
