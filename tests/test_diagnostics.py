import json
import unittest

from agent_harness.diagnostics import build_support_snapshot, validate_public_snapshot
from agent_harness.health import HealthCode, component, report


class DiagnosticsTests(unittest.TestCase):
    def test_snapshot_is_allowlist_only_and_json_serializable(self):
        health = report([component("hub", "healthy", HealthCode.OK, "private message should be omitted")])
        # Accidental inputs outside explicit allowlists are ignored.
        health["private_key"] = "SECRET"
        health["server"] = "https://private.invalid"
        snapshot = build_support_snapshot(
            health,
            app_version="1.3.0",
            capabilities={"sse": True, "bad": "secret"},
            counts={"receivers": 2, "negative": -1, "bad": "secret"},
        )
        encoded = json.dumps(snapshot, ensure_ascii=False)
        for secret in ("SECRET", "private.invalid", "private message should be omitted"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(snapshot["capabilities"], {"sse": True})
        self.assertEqual(snapshot["counts"], {"receivers": 2})
        self.assertTrue(validate_public_snapshot(snapshot))

    def test_validator_rejects_nested_forbidden_fields(self):
        with self.assertRaises(ValueError):
            validate_public_snapshot({"safe": [{"device_token": "secret"}]})

    def test_invalid_health_is_rejected(self):
        with self.assertRaises(ValueError):
            build_support_snapshot({"schema_version": 999, "components": []})


if __name__ == "__main__":
    unittest.main()
