import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.ai_setup import PLACEHOLDER, render_linux_setup_prompt
from agent_harness.api_contracts import DESKTOP_CONTROL
from agent_harness.desktop_actions import DesktopIdentityActions
from agent_harness.desktop_runtime import web_assets
from agent_harness.linux_receiver import build_parser
from scripts import build_linux_receiver


class LinuxAISetupTests(unittest.TestCase):
    def test_rendered_prompt_uses_one_approved_https_service(self):
        server = "https://team.example.ts.net"
        prompt = render_linux_setup_prompt(web_assets() / "linux-ai-setup.md", server)
        self.assertIn(server, prompt)
        self.assertNotIn(PLACEHOLDER, prompt)
        for command in (
            "./harness link",
            "./harness doctor",
            "./harness service install",
            "./harness service status",
            "./harness codex connect",
        ):
            self.assertIn(command, prompt)
        for safety_rule in (
            "不自动使用 `sudo`",
            "不自动执行 `loginctl enable-linger`",
            "不自动使用 `--no-password`",
            "保持当前 link 进程运行并等待批准",
        ):
            self.assertIn(safety_rule, prompt)

    def test_renderer_rejects_loopback_or_invalid_template(self):
        template = web_assets() / "linux-ai-setup.md"
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            render_linux_setup_prompt(template, "http://127.0.0.1:8765")
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.md"
            invalid.write_text("no placeholder", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "模板无效"):
                render_linux_setup_prompt(invalid, "https://team.example.ts.net")

    def test_desktop_action_only_uses_an_account_service(self):
        class Bridge:
            identity = {"account_id": "account"}

            @staticmethod
            def _services():
                return {"https://team.example.ts.net"}

        actions = DesktopIdentityActions(Bridge())
        result = actions.invoke("linux-setup-prompt", {"server": "https://team.example.ts.net/"})
        self.assertIn("team.example.ts.net", result["prompt"])
        with self.assertRaisesRegex(ValueError, "当前账户"):
            actions.invoke("linux-setup-prompt", {"server": "https://other.example.ts.net"})

    def test_desktop_contract_requires_bounded_server_text(self):
        with self.assertRaisesRegex(ValueError, "requires fields: server"):
            DESKTOP_CONTROL.validate("linux-setup-prompt", {})
        DESKTOP_CONTROL.validate("linux-setup-prompt", {"server": "https://team.example.ts.net"})
        with self.assertRaisesRegex(ValueError, "too long"):
            DESKTOP_CONTROL.validate("linux-setup-prompt", {"server": "x" * 2049})

    def test_commands_referenced_by_the_runbook_exist(self):
        parser = build_parser()
        for arguments in (
            ["link", "--server", "https://team.example.ts.net", "--codex", "/opt/codex"],
            ["doctor"],
            ["service", "install"],
            ["service", "status"],
            ["service", "logs", "--lines", "100"],
            ["codex", "connect"],
            ["codex", "status"],
        ):
            with self.subTest(arguments=arguments):
                self.assertIsNotNone(parser.parse_args(arguments).command)

    def test_linux_archive_contains_the_ai_runbook(self):
        def fake_runtime(work):
            runtime = work / "runtime"
            runtime.mkdir()
            (runtime / "harness").write_text("binary", encoding="utf-8")
            return runtime

        def fake_service_bundle(root):
            python = root / "runtime" / "bin" / "python3"
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
            client = root / "client" / "agent_harness"
            client.mkdir(parents=True)
            (client / "desktop_service.py").write_text("service", encoding="utf-8")
            (client / "headless_transfers.py").write_text("transfers", encoding="utf-8")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(build_linux_receiver.sys, "platform", "linux"),
            patch.object(build_linux_receiver, "_architecture", return_value="x64"),
            patch.object(build_linux_receiver, "_build_runtime", side_effect=fake_runtime),
            patch.object(build_linux_receiver, "_build_service_bundle", side_effect=fake_service_bundle),
        ):
            archive = build_linux_receiver.build(Path(directory))
            with tarfile.open(archive, "r:gz") as bundle:
                names = set(bundle.getnames())
                self.assertIn("AgentHarness-linux/交给AI安装.md", names)
                self.assertIn("AgentHarness-linux/runtime/bin/python3", names)
                self.assertIn("AgentHarness-linux/client/agent_harness/desktop_service.py", names)
                self.assertIn("AgentHarness-linux/client/agent_harness/headless_transfers.py", names)
                prompt = bundle.extractfile("AgentHarness-linux/交给AI安装.md").read().decode("utf-8")
            self.assertEqual(prompt.count(PLACEHOLDER), 1)


if __name__ == "__main__":
    unittest.main()
