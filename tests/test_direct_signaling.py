"""Signaling: the service routes offers and state, never the file itself."""

import unittest

from agent_harness.direct_signaling import Transfers
from tests import test_collaboration as fixtures


class TransferSignalingTests(unittest.TestCase):
    setUp = fixtures.CollaborationTests.setUp
    tearDown = fixtures.CollaborationTests.tearDown

    def signals(self):
        return Transfers(self.store)

    def peer_node(self, name="peer-node", owner=None):
        owner = owner or self.other["id"]
        with self.store.connect() as db:
            db.execute("INSERT INTO nodes VALUES(?,?)", (name, owner))
        return name

    def test_devices_of_one_account_transfer_without_an_acceptance_step(self):
        fixtures.PullBackend(self.store).create_pair(self.owner["id"], "desk")
        offer = self.signals().offer(self.owner["id"], "lab", "desk", "dump.bin", 2048, "100.64.0.2:5000")
        self.assertEqual(offer["state"], "accepted")

    def test_a_teammate_decides_for_a_transfer_across_accounts(self):
        node = self.peer_node()
        offer = self.signals().offer(self.owner["id"], "lab", node, "dump.bin", 2048, "100.64.0.2:5000")
        self.assertEqual(offer["state"], "queued")
        # The sender may not answer its own request.
        with self.assertRaises(PermissionError):
            self.signals().respond(self.owner["id"], offer["id"], True)
        self.assertEqual(self.signals().respond(self.other["id"], offer["id"], True)["state"], "accepted")

    def test_a_teammate_can_refuse(self):
        node = self.peer_node()
        offer = self.signals().offer(self.owner["id"], "lab", node, "dump.bin", 2048, "100.64.0.2:5000")
        self.assertEqual(self.signals().respond(self.other["id"], offer["id"], False)["state"], "rejected")

    def test_a_stranger_cannot_offer_or_respond(self):
        node = self.peer_node()
        offer = self.signals().offer(self.owner["id"], "lab", node, "dump.bin", 2048, "100.64.0.2:5000")
        with self.assertRaises(PermissionError):
            self.signals().respond(self.owner["id"], offer["id"], True)
        with self.assertRaises(PermissionError):
            self.signals().offer(self.other["id"], "lab", node, "x", 10, "1.2.3.4:1")

    def test_invalid_offers_are_refused(self):
        cases = (
            {"name": "", "size": 10, "address": "1.2.3.4:1"},
            {"name": "x", "size": 0, "address": "1.2.3.4:1"},
            {"name": "x", "size": True, "address": "1.2.3.4:1"},
            {"name": "x", "size": 10, "address": "not a host"},
            {"name": "x", "size": 10, "address": "1.2.3.4:1", "to_node": "lab"},
        )
        for values in cases:
            to_node = values.pop("to_node", "desk")
            fixtures.PullBackend(self.store).create_pair(self.owner["id"], "desk")
            with self.assertRaises(ValueError):
                self.signals().offer(self.owner["id"], "lab", to_node, **values)

    def test_progress_is_visible_to_both_sides_and_carries_no_content(self):
        node = self.peer_node()
        offer = self.signals().offer(self.owner["id"], "lab", node, "dump.bin", 2048, "100.64.0.2:5000")
        self.signals().respond(self.other["id"], offer["id"], True)
        self.signals().progress(self.owner["id"], offer["id"], 1024, "running")
        self.signals().progress(self.other["id"], offer["id"], 2048, "done")
        view = self.signals().view(self.owner["id"])[0]
        self.assertEqual(view["state"], "done")
        self.assertEqual(view["transferred"], 2048)
        self.assertNotIn("content", view)
        self.assertEqual(len(self.signals().view(self.other["id"])), 1)

    def test_the_sender_chooses_the_id_and_the_service_keeps_it(self):
        # The sender has already built its URL and its signature around this id;
        # a second id minted here could never match what it serves.
        fixtures.PullBackend(self.store).create_pair(self.owner["id"], "desk")
        chosen = "transfer-" + "a" * 32
        offer = self.signals().offer(
            self.owner["id"], "lab", "desk", "dump.bin", 2048, "100.64.0.2:5000", transfer_id=chosen
        )
        self.assertEqual(offer["id"], chosen)
        self.assertEqual(self.signals().view(self.owner["id"])[0]["id"], chosen)

    def test_a_malformed_or_reused_id_is_refused(self):
        fixtures.PullBackend(self.store).create_pair(self.owner["id"], "desk")
        with self.assertRaises(ValueError):
            self.signals().offer(self.owner["id"], "lab", "desk", "x", 10, "1.2.3.4:1", transfer_id="transfer-1")
        chosen = "transfer-" + "b" * 32
        self.signals().offer(self.owner["id"], "lab", "desk", "x", 10, "1.2.3.4:1", transfer_id=chosen)
        with self.assertRaises(ValueError):
            self.signals().offer(self.owner["id"], "lab", "desk", "y", 10, "1.2.3.4:1", transfer_id=chosen)

    def test_the_claiming_device_publishes_its_signing_key(self):
        node = self.peer_node()
        offer = self.signals().offer(
            self.owner["id"], "lab", None, "dump.bin", 2048, "100.64.0.2:5000", recipient=self.other["id"]
        )
        self.assertEqual(offer["state"], "queued")
        accepted = self.signals().respond(self.other["id"], offer["id"], True, node=node, claim_key="k" * 44)
        self.assertEqual(accepted["to_node"], node)
        view = self.signals().view(self.owner["id"])[0]
        self.assertEqual(view["claim_key"], "k" * 44)
        self.assertEqual(view["to_node"], node)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
