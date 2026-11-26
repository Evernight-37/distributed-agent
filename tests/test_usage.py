"""Check provider reporting boundaries where an apparent total can mislead."""

import unittest

from agent_harness.usage import CliUsage, execution_telemetry


class UsageTests(unittest.TestCase):
    def test_cold_codex_reports_usage_only_with_a_known_thread_baseline(self):
        event = {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 40, "output_tokens": 8}}
        for resumed in (False, True):
            usage = CliUsage("codex", resumed=resumed)
            usage.observe(event)
            self.assertEqual(usage.result(), {} if resumed else event["usage"])

    def test_missing_dsh_step_usage_never_publishes_a_partial_total(self):
        usage = CliUsage("dsh", resumed=True)
        usage.observe({"type": "status", "phase": []})
        step = {"type": "status", "phase": "step_end", "turn": 1, "step": 1}
        usage.observe({**step, "usage": {"inputTokens": 8, "outputTokens": 4, "totalTokens": 20}})
        self.assertEqual(usage.result(), {"input_tokens": 16, "output_tokens": 4})
        usage.observe({**step, "step": 2})
        self.assertEqual(usage.result(), {})

    def test_invalid_optional_telemetry_does_not_fabricate_a_rate_or_fail_the_report(self):
        self.assertEqual(
            execution_telemetry(
                {
                    "started_at": "1970-01-01T00:00:10+00:00",
                    "finished_at": 5,
                    "usage": {"input_tokens": 10, "cached_input_tokens": 80, "output_tokens": True},
                }
            ),
            {"started_at": 10, "usage": {"input_tokens": 10}},
        )
