import unittest
from unittest import mock

import worker


class WorkerTimeoutTests(unittest.TestCase):
    def test_managed_and_ordinary_turns_honor_configured_timeout(self):
        for managed in (False, True):
            config = {"project": {"timeout": 600}, "managed_turn": managed}
            self.assertEqual(worker.execution_timeout(config), 600)

    def test_timeout_must_be_positive(self):
        with self.assertRaises(ValueError):
            worker.execution_timeout({"project": {"timeout": 0}})

    def test_cli_version_probe_replaces_invalid_oem_bytes(self):
        completed = mock.Mock(returncode=0, stdout="codex-cli 1.2.3\ufffd", stderr="")
        with mock.patch.object(worker.subprocess, "run", return_value=completed) as run:
            self.assertEqual(worker.codex_version("codex.cmd"), (1, 2, 3))
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_rpc_probe_decodes_version_defensively(self):
        with (
            mock.patch.object(worker, "cli_path", return_value="codex.cmd"),
            mock.patch.object(worker.subprocess, "check_output", return_value="codex-cli 1.2.3\ufffd") as probe,
        ):
            self.assertIn("codex-cli 1.2.3", worker.rpc({"action": "probe"})["version"])
        self.assertEqual(probe.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(probe.call_args.kwargs["errors"], "replace")


if __name__ == "__main__":
    unittest.main()
