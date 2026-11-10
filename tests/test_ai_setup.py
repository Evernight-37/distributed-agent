"""The AI install runbooks: one per platform, all rendered from the app."""

import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.ai_setup import PLACEHOLDER, RUNBOOKS, render_setup_prompt
from agent_harness.api_contracts import DESKTOP_CONTROL
from agent_harness.desktop_actions import DesktopIdentityActions
from agent_harness.desktop_runtime import web_assets
from agent_harness.linux_receiver import build_parser
from scripts import build_linux_receiver

SERVER = "https://team.example.ts.net"

EXPECTED = {
    "linux": (
        "./harness link",
        "./harness doctor",
        "./harness service install",
        "不自动使用 `sudo`",
        "保持当前 link 进程运行并等待批准",
    ),
    "windows": (
        "Expand-Archive",
        "Get-FileHash",
        "Start-Process",
        "不自动请求管理员权限",
        "不静默安装",
        "不添加防火墙规则",
    ),
    "macos": (
        "ditto AgentHarness.app",
        "shasum -a 256 -c",
        "open ~/Applications/AgentHarness.app",
        "不要自动移除隔离属性",
        "不自动使用 `sudo`",
    ),
}


class AISetupRunbookTests(unittest.TestCase):
    def test_every_platform_has_a_runbook(self):
        for platform, name in RUNBOOKS.items():
            with self.subTest(platform=platform):
                self.assertTrue((web_assets() / name).is_file(), name)

    def test_each_runbook_renders_with_one_approved_service(self):
        for platform, name in RUNBOOKS.items():
            with self.subTest(platform=platform):
                prompt = render_setup_prompt(web_assets() / name, SERVER)
                self.assertIn(SERVER, prompt)
                self.assertNotIn(PLACEHOLDER, prompt)
                for expected in EXPECTED[platform]:
                    self.assertIn(expected, prompt)

    def test_renderer_rejects_loopback_or_invalid_template(self):
        template = web_assets() / RUNBOOKS["windows"]
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            render_setup_prompt(template, "http://127.0.0.1:8765")
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.md"
            invalid.write_text("no placeholder", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "模板无效"):
                render_setup_prompt(invalid, SERVER)

    def test_the_action_serves_every_platform_from_an_account_service(self):
        class Bridge:
            identity = {"account_id": "account"}

            @staticmethod
            def _services():
                return {SERVER}

        actions = DesktopIdentityActions(Bridge())
        for platform in RUNBOOKS:
            with self.subTest(platform=platform):
                result = actions.invoke("ai-setup-prompt", {"server": SERVER + "/", "platform": platform})
                self.assertEqual(result["platform"], platform)
                self.assertIn("team.example.ts.net", result["prompt"])
        with self.assertRaisesRegex(ValueError, "未知的目标平台"):
            actions.invoke("ai-setup-prompt", {"server": SERVER, "platform": "solaris"})
        with self.assertRaisesRegex(ValueError, "当前账户"):
            actions.invoke("ai-setup-prompt", {"server": "https://other.example.ts.net", "platform": "linux"})

    def test_desktop_contract_requires_a_bounded_service_and_platform(self):
        with self.assertRaisesRegex(ValueError, "requires fields"):
            DESKTOP_CONTROL.validate("ai-setup-prompt", {"server": SERVER})
        DESKTOP_CONTROL.validate("ai-setup-prompt", {"server": SERVER, "platform": "macos"})
        with self.assertRaises(ValueError):
            DESKTOP_CONTROL.validate("ai-setup-prompt", {"server": SERVER, "platform": "solaris"})
        with self.assertRaisesRegex(ValueError, "too long"):
            DESKTOP_CONTROL.validate("ai-setup-prompt", {"server": "x" * 2049, "platform": "linux"})

    def test_commands_referenced_by_the_linux_runbook_exist(self):
        parser = build_parser()
        for arguments in (
            ["link", "--server", SERVER, "--codex", "/opt/codex"],
            ["doctor"],
            ["service", "install"],
            ["service", "status"],
            ["service", "logs", "--lines", "100"],
            ["codex", "connect"],
            ["codex", "status"],
        ):
            with self.subTest(arguments=arguments):
                self.assertIsNotNone(parser.parse_args(arguments).command)

    def test_linux_bundle_carries_its_runbook(self):
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
            (client / "pull_worker.py").write_text("worker", encoding="utf-8")

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
                self.assertIn("AgentHarness-linux/client/agent_harness/desktop_service.py", names)
                prompt = bundle.extractfile("AgentHarness-linux/交给AI安装.md").read().decode("utf-8")
            self.assertEqual(prompt.count(PLACEHOLDER), 1)


if __name__ == "__main__":
    unittest.main()
