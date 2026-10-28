import copy
import unittest

from agent_harness.account_identity import generate_identity, sign_envelope, verify_envelope
from agent_harness.account_recovery import create_recovery_package, recover_device


class AccountRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = generate_identity()

    def test_package_contains_no_plain_private_key_and_recovers_new_child(self):
        package = create_recovery_package(self.root, "recovery password", created_at=1)
        self.assertNotIn(self.root["private_key"], str(package))
        child = recover_device(package, "recovery password")
        self.assertEqual(child["account_id"], self.root["account_id"])
        self.assertNotEqual(child["public_key"], self.root["public_key"])
        verified = verify_envelope(sign_envelope(child, {"ok": True}), self.root["account_id"])
        self.assertEqual(verified["root_public_key"], self.root["public_key"])

    def test_wrong_password_and_tampering_are_rejected(self):
        package = create_recovery_package(self.root, "correct")
        with self.assertRaises(PermissionError):
            recover_device(package, "wrong")
        tampered = copy.deepcopy(package)
        tampered["ciphertext"] = tampered["ciphertext"][:-4] + "AAAA"
        with self.assertRaises((PermissionError, ValueError)):
            recover_device(tampered, "correct")

    def test_child_device_cannot_export_root_recovery(self):
        child = recover_device(create_recovery_package(self.root, "correct"), "correct")
        with self.assertRaises(PermissionError):
            create_recovery_package(child, "another")

    def test_empty_password_is_rejected_without_length_policy(self):
        with self.assertRaises(ValueError):
            create_recovery_package(self.root, "")
        package = create_recovery_package(self.root, "x")
        self.assertEqual(recover_device(package, "x")["account_id"], self.root["account_id"])


if __name__ == "__main__":
    unittest.main()
