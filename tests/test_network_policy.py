"""The machine owns the proxy endpoint; the agent owns whether to use it."""

import tempfile
import unittest
from pathlib import Path

from agent_harness import executors
from agent_harness.common import write_json


class NetworkPolicyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        write_json(self.root / "executor.json", {"proxy": {"enabled": True, "port": 7897}})

    def environment(self, network=None):
        return executors.child_environment(self.root, network=network)

    def test_a_machine_with_a_proxy_serves_agents_that_follow_it(self):
        env = self.environment({"mode": "machine"})
        self.assertIn("7897", env.get("HTTP_PROXY", ""))
        self.assertIn("7897", env.get("HTTPS_PROXY", ""))

    def test_an_agent_can_stay_direct_on_a_machine_that_has_a_proxy(self):
        env = self.environment({"mode": "direct"})
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("ALL_PROXY", env)

    def test_an_agent_can_point_at_its_own_port(self):
        env = self.environment({"mode": "custom", "port": 1080})
        self.assertIn("1080", env.get("HTTPS_PROXY", ""))

    def test_no_policy_means_the_machine_setting(self):
        self.assertIn("7897", self.environment(None).get("HTTPS_PROXY", ""))

    def test_a_disabled_machine_proxy_injects_nothing(self):
        write_json(self.root / "executor.json", {"proxy": {"enabled": False, "port": None}})
        env = self.environment({"mode": "machine"})
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)

    def test_invalid_policies_are_refused(self):
        for value in (
            {"mode": "sometimes"},
            {"mode": "custom"},
            {"mode": "custom", "port": 0},
            {"mode": "custom", "port": 70000},
            {"mode": "custom", "port": "7897"},
            {"mode": "machine", "host": "10.0.0.1"},
        ):
            with self.assertRaises(ValueError):
                executors.resolve_network(self.root, value)

    def test_no_machine_endpoint_resolves_to_nothing(self):
        write_json(self.root / "executor.json", {})
        self.assertIsNone(executors.resolve_network(self.root, {"mode": "machine"}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
