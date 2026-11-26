"""The private Codex MCP adapter for a single receiver job."""

import argparse
import json
import sys
from pathlib import Path

# Codex launches an absolute script with -I so the model's working directory
# and PYTHONPATH cannot replace the receiver's package.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_harness.common import read_json
from agent_harness.project_actions import project_tool_action, resolve_tool_action, tool_specs


SERVER_NAME = "foolship_project"


def server_settings(path):
    command = str(Path(sys.executable).resolve())
    args = ["-I", "-X", "utf8", str(Path(__file__).resolve()), str(Path(path).resolve())]
    # This adapter only exposes the Hub's validated actions for one job. An
    # unattended Codex turn cannot answer a second confirmation for those tools.
    return {"command": command, "args": args, "enabled": True, "default_tools_approval_mode": "approve"}


def server_override(settings):
    # JSON's quoted string and string-array forms also work in these TOML values.
    command = json.dumps(settings["command"], ensure_ascii=False)
    args = json.dumps(settings["args"], ensure_ascii=False)
    approval = json.dumps(settings["default_tools_approval_mode"])
    return (
        f"mcp_servers.{SERVER_NAME}={{command={command},args={args},enabled=true,"
        f"default_tools_approval_mode={approval}}}"
    )


class ProjectTools:
    def __init__(self, path):
        from agent_harness import worker

        self.path = Path(path).resolve()
        config = read_json(self.path / "request.json", {})
        self.policy = worker.project_action_policy(config)
        self.tools = {}
        contract = worker.contract_spec(config)
        self.file_delivery = contract.get("project_file_delivery") is True
        if not config.get("strict_guard") and contract.get("project_tools") is True:
            self.tools = {spec["name"]: spec for spec in tool_specs(self.policy, file_delivery=self.file_delivery)}
        self.invoke = worker.invoke_project_tool

    def tool_specs(self):
        return self.tools.values()

    def call(self, name, arguments):
        if name not in self.tools:
            return {"ok": False, "error_code": "action_not_allowed", "message": "本轮没有此任务工具。"}
        try:
            action = project_tool_action(name, arguments)
            resolve_tool_action(action, self.policy, file_delivery=self.file_delivery)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error_code": "invalid_arguments", "message": str(exc)}
        return self.invoke(self.path, action)


def main():
    from agent_harness.mcp_server import run_stdio

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_path", type=Path)
    args = parser.parse_args()
    run_stdio(ProjectTools(args.job_path))


if __name__ == "__main__":
    main()
