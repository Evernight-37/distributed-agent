import io
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_harness import executors, warm_worker, worker
from agent_harness.common import write_json
from agent_harness.mcp_server import run_stdio
from agent_harness.project_tool_mcp import SERVER_NAME, ProjectTools, server_override, server_settings


class ProjectToolMcpTests(unittest.TestCase):
    def test_registered_tool_returns_hub_success_and_conflict_in_same_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            policy = {"role": "executor"}
            write_json(
                path / "request.json",
                {
                    "prompt": json.dumps(
                        {
                            "project_tools": True,
                            "project_action_policy": policy,
                        }
                    )
                },
            )
            incoming = io.StringIO(
                "\n".join(
                    json.dumps(request)
                    for request in (
                        {"id": 1, "method": "tools/list"},
                        {
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "project_task_deliver",
                                "arguments": {"summary": "Result", "links": []},
                            },
                        },
                        {"id": 3, "method": "tools/call", "params": {"name": "project_task_deliver"}},
                        {"id": 4, "method": "tools/call", "params": {"name": "project_draft_create"}},
                        {
                            "id": 5,
                            "method": "tools/call",
                            "params": {
                                "name": "project_task_deliver",
                                "arguments": {"summary": "Repeat", "links": []},
                            },
                        },
                    )
                )
            )
            outgoing = io.StringIO()
            with patch.object(
                worker,
                "invoke_project_tool",
                create=True,
                side_effect=[
                    {"ok": True, "status": "delivered"},
                    {"ok": False, "error_code": "already_submitted", "message": "保存过了"},
                ],
            ) as invoke:
                run_stdio(ProjectTools(path), incoming, outgoing)
                invoke.assert_any_call(path.resolve(), {"kind": "deliver", "summary": "Result", "links": []})
                self.assertEqual(invoke.call_count, 2)
            responses = [json.loads(line)["result"] for line in outgoing.getvalue().splitlines()]
            self.assertEqual([item["name"] for item in responses[0]["tools"]], ["project_task_deliver"])
            self.assertEqual(responses[1]["structuredContent"], {"ok": True, "status": "delivered"})
            self.assertEqual(responses[2]["structuredContent"]["error_code"], "invalid_arguments")
            self.assertIn("summary", responses[2]["structuredContent"]["message"])
            self.assertEqual(responses[3]["structuredContent"]["error_code"], "action_not_allowed")
            self.assertEqual(responses[4]["structuredContent"]["error_code"], "already_submitted")

    def test_server_without_marker_or_with_strict_guard_lists_no_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for marker, guard in ((False, None), (True, {"strict": True})):
                with self.subTest(marker=marker, guard=guard):
                    write_json(
                        path / "request.json",
                        {
                            "prompt": json.dumps(
                                {"project_action_policy": {"role": "executor"}, "project_tools": marker}
                            ),
                            "strict_guard": guard,
                        },
                    )
                    with patch.object(worker, "invoke_project_tool", create=True) as invoke:
                        tools = ProjectTools(path)
                        self.assertEqual(list(tools.tool_specs()), [])
                        result = tools.call("project_task_deliver", {"summary": "Result", "links": []})
                        self.assertFalse(result["ok"])
                        invoke.assert_not_called()

    def test_cold_codex_injects_only_the_requested_job_server(self):
        project = {"model": "example", "effort": "low", "remote_path": "workspace", "local_control": True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job with spaces and 中文"
            settings = server_settings(path)
            override = server_override(settings)
            args = executors.command(path, "codex", project, None, path, True, kind=executors.CODEX, project_tools=True)
            self.assertEqual(args[args.index(override) - 1], "-c")
            self.assertIn("--ignore-user-config", args)
            self.assertEqual(settings["args"][-1], str(path.resolve()))
            self.assertTrue(override.startswith("mcp_servers." + SERVER_NAME + "="))
            self.assertEqual(tomllib.loads(override)["mcp_servers"], {SERVER_NAME: settings})
            self.assertEqual(settings["default_tools_approval_mode"], "approve")
            self.assertIn('approval_policy="never"', args)
            ordinary = executors.command(path, "codex", project, None, path, True, kind=executors.CODEX)
            self.assertNotIn(override, ordinary)

    def test_warm_server_restarts_when_the_job_scope_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_scope = server_settings(root / "first")
            second_scope = server_settings(root / "second")
            enabled = {server_override(scope): scope for scope in (first_scope, second_scope)}

            def make_server(command, *unused):
                server = Mock()
                server.process.poll.return_value = None
                scope = next((enabled[arg] for arg in command if arg in enabled), None)
                servers = {SERVER_NAME: scope or {"enabled": False}, "inherited": {"enabled": False}}
                server.call.return_value = {"config": {"mcp_servers": servers}}
                server.command = command
                return server

            with (
                patch.object(warm_worker, "project_dir", return_value=root),
                patch.object(warm_worker, "server_command", side_effect=lambda stage: ["codex"]),
                patch.object(warm_worker, "local_env", return_value={}),
                patch.object(warm_worker, "mcp_names", return_value=[SERVER_NAME, "inherited"]),
                patch.object(warm_worker, "AppServer", side_effect=make_server),
            ):
                manager = warm_worker.WarmManager("project")
                try:
                    first = manager.ensure_server("discuss", str(root), first_scope)
                    self.assertIs(manager.ensure_server("discuss", str(root), first_scope), first)
                    second = manager.ensure_server("discuss", str(root), second_scope)
                    first.close.assert_called_once()
                    self.assertIn(server_override(second_scope), second.command)
                    self.assertIn('mcp_servers.inherited={command="",enabled=false}', second.command)
                    ordinary = manager.ensure_server("discuss", str(root))
                    second.close.assert_called_once()
                    self.assertIn('mcp_servers.foolship_project={command="",enabled=false}', ordinary.command)
                finally:
                    manager.close_server()
