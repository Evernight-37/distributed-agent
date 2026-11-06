"""Sealing keeps owner-command payloads readable only by the target machine."""

import unittest

from agent_harness import sealing


class SealingTests(unittest.TestCase):
    def setUp(self):
        self.device = sealing.generate_keypair()
        self.other = sealing.generate_keypair()

    def test_a_payload_round_trips_for_the_intended_device(self):
        context = {"target": "node-a", "id": "command-1"}
        sealed = sealing.seal(self.device["public_key"], {"port": 7897, "enabled": True}, context)
        self.assertEqual(
            sealing.open_sealed(self.device["private_key"], sealed, context),
            {"enabled": True, "port": 7897},
        )

    def test_the_service_never_sees_the_plaintext(self):
        sealed = sealing.seal(self.device["public_key"], {"port": 7897})
        self.assertNotIn(b"7897", str(sealed).encode("utf-8"))

    def test_another_device_cannot_open_it(self):
        sealed = sealing.seal(self.device["public_key"], {"port": 7897})
        with self.assertRaises(ValueError):
            sealing.open_sealed(self.other["private_key"], sealed)

    def test_a_payload_moved_to_another_command_is_rejected(self):
        sealed = sealing.seal(self.device["public_key"], {"port": 7897}, {"target": "node-a", "id": "command-1"})
        with self.assertRaises(ValueError):
            sealing.open_sealed(self.device["private_key"], sealed, {"target": "node-b", "id": "command-1"})

    def test_a_tampered_ciphertext_is_rejected(self):
        sealed = sealing.seal(self.device["public_key"], {"port": 7897})
        raw = bytearray(sealed["ciphertext"].encode("ascii"))
        raw[-2] = ord("A") if raw[-2] != ord("A") else ord("B")
        sealed["ciphertext"] = raw.decode("ascii")
        with self.assertRaises(ValueError):
            sealing.open_sealed(self.device["private_key"], sealed)

    def test_malformed_shapes_are_rejected(self):
        for value in (None, {}, {"version": 1}, {"version": 2, "epk": "", "nonce": "", "ciphertext": ""}):
            with self.assertRaises(ValueError):
                sealing.open_sealed(self.device["private_key"], value)

    def test_an_oversized_payload_is_refused(self):
        with self.assertRaises(ValueError):
            sealing.seal(self.device["public_key"], {"blob": "x" * (sealing.MAX_PLAINTEXT + 1)})
