import unittest
from unittest import mock

from agent_harness import protocol


class ProtocolCompatibilityTests(unittest.TestCase):
    def test_legacy_hub_is_accepted_during_transition(self):
        self.assertEqual(protocol.ensure_hub_compatible({"features": []}), {"legacy": True})

    def test_current_hub_advertisement_is_accepted(self):
        advertisement = protocol.hub_advertisement()
        self.assertEqual(
            protocol.ensure_hub_compatible({"protocol": advertisement})["hub"],
            protocol.HUB_PROTOCOL,
        )

    def test_too_old_hub_and_too_old_client_fail_clearly(self):
        with mock.patch.object(protocol, "MINIMUM_HUB_PROTOCOL", 2):
            with self.assertRaisesRegex(RuntimeError, "托管端"):
                protocol.ensure_hub_compatible({"protocol": {"hub": 1, "minimum_client": 1}})
        with self.assertRaisesRegex(RuntimeError, "升级本机"):
            protocol.ensure_hub_compatible(
                {
                    "protocol": {
                        "hub": protocol.HUB_PROTOCOL,
                        "minimum_client": protocol.CLIENT_PROTOCOL + 1,
                    }
                }
            )

    def test_malformed_protocol_fails_closed(self):
        for value in (
            "1",
            {},
            {"hub": True, "minimum_client": 1},
            {"hub": 1, "minimum_client": "1"},
        ):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    protocol.ensure_hub_compatible({"protocol": value})


if __name__ == "__main__":
    unittest.main()
