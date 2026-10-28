import unittest

from agent_harness.health import (
    MAX_HEALTH_MESSAGE,
    HealthCode,
    component,
    normalize_exception,
    report,
    safe_message,
)
from agent_harness.node_api import NodeLocalActions


class HealthTests(unittest.TestCase):
    def test_report_has_stable_closed_schema(self):
        value = report(
            [
                component("hub", "healthy", HealthCode.OK, "运行正常"),
                component(
                    "network",
                    "unavailable",
                    HealthCode.NETWORK_UNREACHABLE,
                    "无法连接",
                    True,
                ),
            ],
            generated_at=1,
        )
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["overall"], "unavailable")
        self.assertEqual(set(value), {"schema_version", "generated_at", "overall", "components"})
        self.assertEqual(
            set(value["components"][0]),
            {"id", "state", "code", "message", "retryable", "updated_at"},
        )

    def test_messages_are_bounded_and_redact_addresses_paths_and_secrets(self):
        raw = (
            "token=abc123 https://user:pass@example.invalid/a "
            "C:\\Users\\alice\\secret.txt /home/alice/private/value " + "x" * 1000
        )
        value = safe_message(raw)
        self.assertLessEqual(len(value), MAX_HEALTH_MESSAGE)
        for secret in ("abc123", "user:pass", "alice", "secret.txt"):
            self.assertNotIn(secret, value)

    def test_exception_classification_does_not_leak_unknown_exception(self):
        known = normalize_exception("network", TimeoutError("timed out at https://private.invalid"))
        self.assertEqual(known["code"], HealthCode.NETWORK_UNREACHABLE)
        self.assertNotIn("private.invalid", known["message"])
        unknown = normalize_exception("hub", RuntimeError("PRIVATE INTERNAL DETAIL"))
        self.assertEqual(unknown["code"], HealthCode.UNKNOWN)
        self.assertNotIn("PRIVATE INTERNAL DETAIL", unknown["message"])

    def test_invalid_state_and_code_are_rejected(self):
        with self.assertRaises(ValueError):
            component("hub", "bad", HealthCode.OK)
        with self.assertRaises(ValueError):
            component("hub", "healthy", "NOT_A_CODE")

    def test_node_health_does_not_expose_cli_path_or_version_output(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temporary:
            actions = NodeLocalActions(Path(temporary), lambda: "C:/private/codex.exe")
            with patch("agent_harness.node_api.subprocess.check_output", return_value="codex 1.2.3"):
                value = actions.dispatch({"action": "health"})
        self.assertEqual(value["overall"], "healthy")
        self.assertNotIn("C:/private", str(value))


if __name__ == "__main__":
    unittest.main()
