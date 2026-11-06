"""Signaling: the service routes offers and state, never the file itself."""

import unittest

from agent_harness.direct_signaling import Transfers
from tests import test_collaboration as fixtures


class TransferSignalingTests(unittest.TestCase):
    setUp = fixtures.CollaborationTests.setUp
    tearDown = fixtures.CollaborationTests.tearDown

    def signals(self):
        return Transfers(self.store)

    def offer(self, **overrides):
        values = {
            "owner": self.owner["id"],
            "from_node": "lab",
            "to_node": "lab",
            "name": "dump.bin",
            "size": 1024,
            "address": "100.64.0.2:5000",
        }
        values.update(overrides)
        return self.signals().offer(**values)

    def test_a_transfer_between_own_machines_is_accepted_once(self):
        PullBackend = fixtures.PullBackend
        PullBackend(self.store).create_pair(self.owner["id"], "desk")
        offer = self.signals().offer(self.owner["id"], "lab", "desk", "dump.bin", 2048, "100.64.0.2:5000")
        self.assertEqual(offer["state"], "queued")
        accepted = self.signals().respond(self.owner["id"], offer["id"], True)
        self.assertEqual(accepted["state"], "accepted")
        with self.assertRaises(ValueError):
            self.signals().respond(self.owner["id"], offer["id"], True)

    def test_rejecting_ends_the_offer(self):
        fixtures.PullBackend(self.store).create_pair(self.owner["id"], "desk")
        offer = self.signals().offer(self.owner["id"], "lab", "desk", "dump.bin", 2048, "100.64.0.2:5000")
        self.assertEqual(self.signals().respond(self.owner["id"], offer["id"], False)["state"], "rejected")

    def test_another_account_cannot_offer_or_respond(self):
        fixtures.PullBackend(self.store).create_pair(self.owner["id"], "desk")
        with self.assertRaises(PermissionError):
            self.signals().offer(self.other["id"], "lab", "desk", "x", 10, "1.2.3.4:1")
        offer = self.signals().offer(self.owner["id"], "lab", "desk", "dump.bin", 10, "1.2.3.4:1")
        with self.assertRaises(PermissionError):
            self.signals().respond(self.other["id"], offer["id"], True)

    def test_invalid_offers_are_refused(self):
        for values in (
            {"name": ""},
            {"size": 0},
            {"size": True},
            {"address": "not a host"},
            {"to_node": "lab"},
        ):
            with self.assertRaises(ValueError):
                self.offer(**values)

    def test_progress_records_bytes_and_a_terminal_state(self):
        fixtures.PullBackend(self.store).create_pair(self.owner["id"], "desk")
        offer = self.signals().offer(self.owner["id"], "lab", "desk", "dump.bin", 2048, "100.64.0.2:5000")
        self.signals().respond(self.owner["id"], offer["id"], True)
        self.signals().progress(self.owner["id"], offer["id"], 1024, "running")
        self.signals().progress(self.owner["id"], offer["id"], 2048, "done")
        view = self.signals().view(self.owner["id"])[0]
        self.assertEqual(view["state"], "done")
        self.assertEqual(view["transferred"], 2048)
        self.assertNotIn("content", view)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
