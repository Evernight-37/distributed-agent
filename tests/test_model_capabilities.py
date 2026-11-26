import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness import executors, model_capabilities
from agent_harness.appserver_client import AppServer
from agent_harness.common import read_json, write_json


class ModelCapabilitiesTests(unittest.TestCase):
    def test_app_server_reaps_the_process_on_failed_initialization(self):
        with (
            patch("agent_harness.appserver_client.subprocess.Popen") as starter,
            patch("agent_harness.appserver_client.threading.Thread"),
            patch.object(AppServer, "call", side_effect=TimeoutError),
        ):
            process = starter.return_value
            process.poll.return_value = None
            with self.assertRaises(TimeoutError):
                AppServer(["codex"], None, {}, None, initialize_timeout=1)
            process.stdin.close.assert_called_once()
            process.wait.assert_called_once_with(timeout=1)

    def test_codex_reports_only_the_options_for_the_requested_model(self):
        pages = [
            {
                "data": [
                    {"id": "small", "supportedReasoningEfforts": [], "defaultReasoningEffort": None},
                ],
                "nextCursor": "next",
            },
            {
                "data": [
                    {
                        "id": "large",
                        "supportedReasoningEfforts": [{"reasoningEffort": "high"}, {"reasoningEffort": "max"}],
                        "defaultReasoningEffort": "high",
                    }
                ]
            },
        ]
        with tempfile.TemporaryDirectory() as folder, patch.object(model_capabilities, "AppServer") as app:
            server = app.return_value
            server.call.side_effect = pages
            result = model_capabilities._codex_catalog(Path(folder), "codex", ["small", "large"])
        self.assertEqual(result["small"], {"efforts": [], "default_effort": None})
        self.assertEqual(result["large"], {"efforts": ["high", "max"], "default_effort": "high"})
        self.assertTrue(all(call.args[0] == "model/list" for call in server.call.call_args_list))
        server.close.assert_called_once_with(timeout=1)

    def test_dsh_probes_its_exact_provider_without_running_a_turn(self):
        def run(command, **kwargs):
            self.assertEqual(command[:4], ["dsh", "--profile", "headless", "--patch"])
            self.assertEqual(len(command), 5)
            instructions = read_json(Path(command[-1]))
            self.assertIn({"id": "headless-runner", "disabled": True}, instructions)
            configuration = instructions[-1]["insert"][0]["config"]
            self.assertEqual(configuration["provider"], "my-model-provider")
            self.assertEqual(configuration["models"], ["my-model"])
            write_json(
                Path(configuration["result"]), {"my-model": {"efforts": ["off", "high"], "default_effort": "high"}}
            )
            return type("Process", (), {"returncode": 0})()

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(model_capabilities.subprocess, "run", side_effect=run),
        ):
            root = Path(folder)
            write_json(root / "executor.json", {"provider": "my-model-provider"})
            result = model_capabilities._dsh_catalog(root, "dsh", ["my-model"])
        self.assertEqual(result, {"my-model": {"efforts": ["off", "high"], "default_effort": "high"}})

    def test_cache_is_per_receiver_and_invalidation_keeps_unknown_distinct(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(model_capabilities, "_binary", return_value="dsh"),
            patch.object(model_capabilities, "_dsh_catalog") as probe,
        ):
            root = Path(folder)
            settings = root / "executor.json"
            write_json(settings, {"models": [{"name": "my-model", "executor": "dsh"}], "model": "legacy"})
            probe.side_effect = [{}, {"my-model": {"efforts": [], "default_effort": None}}]
            model = model_capabilities.for_model(root, "my-model")
            self.assertEqual(model["legacy_model_override"], "legacy")
            self.assertIsNone(model["efforts"])
            self.assertEqual(model_capabilities.for_model(root, "my-model"), model)
            self.assertEqual(model_capabilities.for_model(root, "my-model", refresh=True)["efforts"], [])
            self.assertEqual(probe.call_count, 2)


class ModelSelectionTests(unittest.TestCase):
    def test_new_dsh_configuration_prefers_project_model_and_passes_native_level(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh", "model": "legacy"})
            project = {"model": "my-model", "effort": "max"}
            self.assertEqual(executors.effective_request(root, project), ("legacy", "high"))
            project["model_config_version"] = 2
            self.assertEqual(executors.effective_request(root, project), ("my-model", "max"))
            scratch = root / "job"
            executors.dsh_command(root, "dsh", project, None, scratch)
            self.assertEqual(read_json(scratch / "dsh-patch.json")[0]["config"]["reasoningEffort"], "max")
            project["effort"] = "default"
            executors.dsh_command(root, "dsh", project, None, scratch)
            self.assertNotIn("reasoningEffort", read_json(scratch / "dsh-patch.json")[0]["config"])

    def test_codex_default_does_not_send_an_effort_override(self):
        args = executors.codex_command("codex", {"model": "model", "remote_path": "/work", "effort": "default"}, None)
        self.assertFalse(any("model_reasoning_effort" in arg for arg in args))
