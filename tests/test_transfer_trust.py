"""The receiving machine remembers which accounts its owner already accepted."""

import tempfile
import unittest
from pathlib import Path

from agent_harness import transfer_trust


class TransferTrustTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_a_new_machine_trusts_nobody(self):
        self.assertEqual(transfer_trust.senders(self.root), [])
        self.assertFalse(transfer_trust.is_trusted(self.root, "account-a"))

    def test_accepting_once_remembers_that_account(self):
        transfer_trust.remember(self.root, "account-a")
        self.assertTrue(transfer_trust.is_trusted(self.root, "account-a"))
        self.assertFalse(transfer_trust.is_trusted(self.root, "account-b"))

    def test_remembering_twice_does_not_duplicate(self):
        transfer_trust.remember(self.root, "account-a")
        transfer_trust.remember(self.root, "account-a")
        self.assertEqual([entry["account"] for entry in transfer_trust.senders(self.root)], ["account-a"])

    def test_forgetting_removes_only_that_account(self):
        transfer_trust.remember(self.root, "account-a")
        transfer_trust.remember(self.root, "account-b")
        transfer_trust.forget(self.root, "account-a")
        self.assertEqual([entry["account"] for entry in transfer_trust.senders(self.root)], ["account-b"])

    def test_a_corrupt_file_reads_as_empty_instead_of_failing(self):
        (self.root / "transfer-trust.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(transfer_trust.senders(self.root), [])
        transfer_trust.remember(self.root, "account-a")
        self.assertTrue(transfer_trust.is_trusted(self.root, "account-a"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
