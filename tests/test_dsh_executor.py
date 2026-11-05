import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import executors
from agent_harness.common import outcome, read_json, write_json

PROJECT = {
    "name": "demo",
    "model": "deepseek-flash",
    "effort": "medium",
    "remote_path": r"C:\work\demo" if os.name == "nt" else "/work/demo",
    "timeout": 600,
    "local_control": True,
}
SESSION = "session-01234567-89ab-cdef-0123-456789abcdef"


def fake_cli(folder, name, version_line):
    """Create a runnable --version stub for the current platform."""
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = directory / (name + ".cmd")
        path.write_bytes(b"@echo off\r\necho " + version_line.encode() + b"\r\n")
        return path
    path = directory / name
    path.write_text("#!/bin/sh\necho " + version_line + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class ExecutorSelectionTests(unittest.TestCase):
    def test_default_selection_is_codex(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(executors.selected(Path(folder)), executors.CODEX)
            self.assertTrue(executors.supports_managed_turns(Path(folder)))

    def test_dsh_selection_disables_the_warm_app_server(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            self.assertEqual(executors.selected(root), executors.DSH)
            self.assertFalse(executors.supports_managed_turns(root))

    def test_unknown_executor_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "gemini"})
            with self.assertRaises(ValueError):
                executors.selected(root)


class ModelTableTests(unittest.TestCase):
    def test_a_machine_without_a_table_gets_the_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            table = executors.models(Path(folder))
            self.assertIn({"name": "gpt-6.1-sol", "executor": "codex"}, table)
            self.assertIn({"name": "deepseek-flash", "executor": "dsh"}, table)

    def test_defaults_only_list_models_this_project_has_shipped(self):
        # A model nobody verified must not become a default: routing it would send
        # real work to a provider route that may not exist.
        names = {entry["name"] for entry in executors.DEFAULT_MODELS}
        self.assertEqual(names, {"gpt-5.6-sol", "gpt-6.1-sol", "gpt-5.6-luna", "deepseek-flash"})

    def test_the_model_decides_which_cli_runs_the_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(executors.model_kind(root, "gpt-6.1-sol"), "codex")
            self.assertEqual(executors.model_kind(root, "deepseek-flash"), "dsh")
            # Case is not significant: agents store what the operator typed.
            self.assertEqual(executors.model_kind(root, "DEEPSEEK-FLASH"), "dsh")
            self.assertIsNone(executors.model_kind(root, "claude-4"))

    def test_a_saved_table_wins_and_drops_broken_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(
                root / "executor.json",
                {
                    "executor": "codex",
                    "models": [
                        {"name": "gpt-5.6-luna", "executor": "codex"},
                        {"name": "deepseek-flash", "executor": "dsh"},
                        {"name": "  ", "executor": "dsh"},
                        {"name": "gpt-5.6-luna", "executor": "dsh"},
                        {"name": "no-cli", "executor": "gemini"},
                    ],
                },
            )
            self.assertEqual(
                executors.models(root),
                [
                    {"name": "gpt-5.6-luna", "executor": "codex"},
                    {"name": "deepseek-flash", "executor": "dsh"},
                ],
            )

    def test_an_empty_table_falls_back_instead_of_blocking_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "codex", "models": []})
            self.assertTrue(executors.models(root))

    def test_dispatch_routes_by_model_and_falls_back_for_unlisted_ones(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(executors.kind_or_selected(root, "deepseek-flash"), "dsh")
            self.assertEqual(executors.kind_or_selected(root, "gpt-6.1-sol"), "codex")
            # An agent configured before the table existed keeps its machine.
            write_json(root / "executor.json", {"executor": "dsh"})
            self.assertEqual(executors.kind_or_selected(root, "brand-new-model"), "dsh")
            # And configuring such a model is still refused up front.
            with self.assertRaises(ValueError):
                executors.kind_for(root, "brand-new-model")


class DshDiscoveryTests(unittest.TestCase):
    def test_explicit_dsh_path_is_probed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            exe = fake_cli(root / "bin", "dsh", "0.2.1-rc.5")
            write_json(root / "executor.json", {"executor": "dsh", "cli_path": str(exe)})
            result = executors.status(root)
            self.assertEqual(
                result,
                {
                    "found": True,
                    "configured": True,
                    "path": str(exe),
                    "version": "0.2.1",
                },
            )

    def test_probing_the_other_executor_ignores_this_ones_saved_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dsh = fake_cli(root / "bin", "dsh", "0.2.1")
            codex = fake_cli(root / "other", "codex", "codex-cli 1.2.3")
            write_json(root / "executor.json", {"executor": "dsh", "cli_path": str(dsh)})
            # Registering the Codex MCP entry must find Codex itself, not the
            # DeepSeek Harness binary this machine happens to run.
            with mock.patch.object(executors, "_codex_candidates", return_value=[codex]):
                result = executors.status_of(root, executors.CODEX)
            self.assertEqual(result["path"], str(codex))
            self.assertEqual(result["version"], "1.2.3")

    def test_missing_dsh_binary_speaks_about_dsh(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            with mock.patch.object(executors, "_dsh_candidates", return_value=[]):
                result = executors.status(root)
            self.assertFalse(result["found"])
            self.assertIn("DeepSeek Harness", result["error"])

    def test_codex_status_shape_is_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            exe = fake_cli(root / "bin", "codex", "codex-cli 1.2.3")
            write_json(root / "executor.json", {"cli_path": str(exe)})
            self.assertEqual(
                executors.status(root),
                {
                    "found": True,
                    "configured": True,
                    "path": str(exe),
                    "version": "1.2.3",
                },
            )


class DshLoginTests(unittest.TestCase):
    def test_missing_api_key_warns_without_blocking(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
                result = executors.login(root, "dsh")
            self.assertTrue(result["logged_in"])
            self.assertIn("DEEPSEEK_API_KEY", result["credential_warning"])

    def test_environment_key_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}):
                self.assertEqual(executors.login(root, "dsh"), {"logged_in": True})


class DshCommandTests(unittest.TestCase):
    def _command(self, root, project=None, thread_id=None):
        scratch = root / "job"
        argv = executors.command(root, "dsh", project or dict(PROJECT), thread_id, scratch=scratch)
        return argv, scratch

    def test_command_shape_and_patch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh", "model": "deepseek-reasoner"})
            argv, scratch = self._command(root)
            self.assertEqual(argv[:2], ["dsh", "--profile"])
            self.assertEqual(argv[2], "headless")
            self.assertIn("--patch", argv)
            self.assertIn("--json", argv)
            self.assertEqual(argv[-1], "-")
            self.assertEqual(argv.count("--session-id"), 0)

            patch = read_json(scratch / "dsh-patch.json")
            by_id = {entry["id"]: entry for entry in patch}
            self.assertEqual(
                by_id["agent-default-model"]["config"],
                {"provider": "deepseek-official", "model": "deepseek-reasoner"},
            )
            self.assertEqual(by_id["approval"]["config"], {"policy": "never"})
            self.assertEqual(
                by_id["permission"]["config"]["presets"]["workspace-write"],
                {"sandbox": "workspace-write", "approval": "never"},
            )
            self.assertFalse(by_id["credentials"]["disabled"])

    def test_project_model_is_used_without_an_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            _, scratch = self._command(root, dict(PROJECT, model="deepseek-v3.2"))
            patch = read_json(scratch / "dsh-patch.json")
            self.assertEqual(patch[0]["config"]["model"], "deepseek-v3.2")

    def test_session_id_resumes_the_provider_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            argv, _ = self._command(root, thread_id=SESSION)
            self.assertEqual(argv[-3:], ["--session-id", SESSION, "-"])

    def test_malformed_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            with self.assertRaises(ValueError):
                self._command(root, thread_id="../../session")

    def test_managed_turn_adds_no_codex_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            argv = executors.command(
                root,
                "dsh",
                dict(PROJECT),
                None,
                scratch=root / "job",
                managed=True,
                schema_path=root / "job" / "reply-schema.json",
            )
            self.assertNotIn("--output-schema", argv)


class CodexCommandTests(unittest.TestCase):
    def test_sandbox_follows_the_stage(self):
        project = dict(PROJECT, model="gpt-5.6-sol", effort="high", _stage="implement")
        argv = executors.codex_command("codex.exe", project, None)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        discuss = dict(project, _stage="discuss")
        argv = executors.codex_command("codex.exe", discuss, None)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")

    def test_managed_turn_disables_extensions_and_pins_a_schema(self):
        project = dict(PROJECT, model="gpt-5.6-sol", effort="high")
        argv = executors.codex_command("codex.exe", project, None, managed=True, schema_path="schema.json")
        self.assertIn("--disable", argv)
        self.assertEqual(argv[argv.index("--output-schema") + 1], "schema.json")
        self.assertEqual(argv[-1], "-")


class DshPermissionEnvironmentTests(unittest.TestCase):
    def test_stage_selects_the_sandbox_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            self.assertEqual(executors.environment(root, "implement")["DSH_PERMISSION_MODE"], "workspace-write")
            self.assertEqual(executors.environment(root, "discuss")["DSH_PERMISSION_MODE"], "read-only")
            self.assertEqual(executors.environment(root, "review")["DSH_PERMISSION_MODE"], "read-only")
            self.assertEqual(
                executors.environment(root, None, full_access=True)["DSH_PERMISSION_MODE"], "danger-full-access"
            )

    def test_codex_receives_no_dsh_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(executors.environment(root, "implement"), {})


class DshEventNormalizationTests(unittest.TestCase):
    def test_headless_json_stream_maps_onto_the_normalized_vocabulary(self):
        stream = [
            {"type": "session", "sessionId": SESSION, "cwd": r"C:\work\demo"},
            {"type": "status", "phase": "turn_start", "turn": 1},
            {"type": "thinking", "text": "reasoning"},
            {"type": "tool_call", "callId": "c1", "tool": "pwsh", "input": {"command": "dir"}},
            {"type": "tool_result", "callId": "c1", "status": "completed", "result": "ok"},
            {"type": "text", "text": "draft"},
            {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
            {"type": "final", "text": '{"reply": "done"}'},
        ]
        normalized = [item for event in stream for item in executors.normalize(executors.DSH, event)]
        self.assertEqual(
            normalized,
            [
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "turn.completed"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": '{"reply": "done"}'}},
            ],
        )
        self.assertEqual(outcome(0, normalized), "needs_review")

    def test_failed_turn_carries_the_provider_error(self):
        event = {
            "type": "status",
            "phase": "turn_end",
            "turn": 1,
            "reason": {
                "kind": "error",
                "error": {"code": "MISSING_CREDENTIAL", "message": "no API key"},
            },
        }
        self.assertEqual(
            executors.normalize(executors.DSH, event),
            [{"type": "turn.failed", "error": "no API key"}],
        )

    def test_runner_error_is_terminal(self):
        self.assertEqual(
            executors.normalize(executors.DSH, {"type": "error", "message": "boom"}),
            [{"type": "turn.failed", "error": "boom"}],
        )

    def test_codex_events_pass_through_untouched(self):
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": "x"}}
        self.assertEqual(executors.normalize(executors.CODEX, event), [event])

    def test_final_without_text_is_still_a_report_slot(self):
        self.assertEqual(
            executors.normalize(executors.DSH, {"type": "final"}),
            [{"type": "item.completed", "item": {"type": "agent_message", "text": ""}}],
        )


class DshEffortTests(unittest.TestCase):
    def test_levels_map_onto_the_provider_vocabulary(self):
        self.assertEqual(executors.reasoning_effort("low"), "low")
        self.assertEqual(executors.reasoning_effort("high"), "high")
        # medium keeps the model's own default instead of guessing a level.
        self.assertIsNone(executors.reasoning_effort("medium"))
        for level in ("xhigh", "max", "ultra"):
            self.assertEqual(executors.reasoning_effort(level), "high")
        self.assertIsNone(executors.reasoning_effort("unknown"))
        self.assertIsNone(executors.reasoning_effort(None))

    def test_patch_carries_the_mapped_effort(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            executors.command(root, "dsh", dict(PROJECT, effort="high"), None, scratch=root / "job")
            patch = read_json(root / "job" / "dsh-patch.json")
            self.assertEqual(patch[0]["config"]["reasoningEffort"], "high")

    def test_medium_omits_the_field_entirely(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            executors.command(root, "dsh", dict(PROJECT, effort="medium"), None, scratch=root / "job")
            patch = read_json(root / "job" / "dsh-patch.json")
            self.assertNotIn("reasoningEffort", patch[0]["config"])

    def test_rejected_effort_is_explained(self):
        message = executors.explain_failure(
            'dsh: UNSUPPORTED_REASONING_EFFORT: provider "deepseek-official" model "deepseek-flash" '
            'does not support reasoning effort "xhigh"'
        )
        self.assertIn("思考强度", message)


class DshModelTests(unittest.TestCase):
    def test_effective_model_is_never_claimed(self):
        self.assertIsNone(executors.effective_model(executors.DSH, SESSION, None))

    def test_requested_model_prefers_the_receiver_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh", "model": "deepseek-reasoner"})
            self.assertEqual(executors.requested_model(root, "gpt-5.6-sol"), "deepseek-reasoner")
            write_json(root / "executor.json", {"executor": "dsh", "model": "   "})
            self.assertEqual(executors.requested_model(root, "deepseek-flash"), "deepseek-flash")

    def test_a_leftover_override_is_ignored_by_codex(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Switching back to Codex leaves the DeepSeek Harness model behind;
            # applying it would fail the effective-model check on every turn.
            write_json(root / "executor.json", {"executor": "codex", "model": "deepseek-reasoner"})
            self.assertEqual(executors.requested_model(root, "gpt-5.6-sol"), "gpt-5.6-sol")
            write_json(root / "executor.json", {"model": "deepseek-reasoner"})
            self.assertEqual(executors.requested_model(root, "gpt-5.6-sol"), "gpt-5.6-sol")


class DshFailureHintTests(unittest.TestCase):
    def test_missing_credential_is_explained_in_chinese(self):
        message = executors.explain_failure("dsh: MISSING_CREDENTIAL: no API key for route")
        self.assertIn("API Key", message)

    def test_unknown_failure_is_not_rewritten(self):
        self.assertIsNone(executors.explain_failure("dsh: something unexpected"))

    def test_stale_session_is_explained(self):
        message = executors.explain_failure(
            'session "session-x" does not exist; omit --session-id to start a new Session'
        )
        self.assertIn("续接", message)


class DshPatchJsonTests(unittest.TestCase):
    def test_patch_is_valid_json_without_yaml_expressions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "executor.json", {"executor": "dsh"})
            executors.command(root, "dsh", dict(PROJECT), None, scratch=root / "job")
            text = (root / "job" / "dsh-patch.json").read_text(encoding="utf-8")
            self.assertNotIn("!!js", text)
            self.assertEqual(
                [entry["id"] for entry in json.loads(text)],
                [
                    "agent-default-model",
                    "approval",
                    "permission",
                    "credentials",
                ],
            )


if __name__ == "__main__":
    unittest.main()
