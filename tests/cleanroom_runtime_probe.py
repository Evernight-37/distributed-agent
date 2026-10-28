"""Probe an unpacked release from its own bundled Python runtime."""

import argparse
import json
import subprocess
from pathlib import Path

from agent_harness.account_identity import generate_identity
from agent_harness.app_bridge import AppBridge, MCP_DELEGATE_SCOPES
from agent_harness.collaboration_store import Store
from agent_harness.common import write_json
from agent_harness.service_install import install_bundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("scratch", type=Path)
    parser.add_argument("expected_version")
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--macos", action="store_true")
    args = parser.parse_args()
    package = args.package.resolve()
    scratch = args.scratch.resolve()

    bridge = AppBridge()
    expected_root = (scratch / "localappdata" / "AgentHarness" / "data").resolve()
    if bridge.root != expected_root or bridge._host_state_root() != expected_root / "team-service":
        raise RuntimeError("fresh data root is not package independent")
    store = Store(bridge._host_state_root())
    if not (expected_root / "team-service" / "collaboration.sqlite3").is_file() or not store.service_id():
        raise RuntimeError("fresh host database was not created in stable state")

    installed = install_bundle(package, scratch / "localappdata" / "AgentHarness" / "service-runtime")
    installed_package = installed / "client" / "agent_harness"
    if (installed_package / "VERSION").read_text(encoding="utf-8").strip() != args.expected_version:
        raise RuntimeError("installed runtime is not this release")
    forbidden = str(args.source_root.resolve())
    for source in installed_package.glob("*.py"):
        if forbidden in source.read_text(encoding="utf-8"):
            raise RuntimeError("source checkout leaked into installed runtime: " + source.name)

    identity = generate_identity()
    identity["account_id"] = "cleanroom-account"
    grant = scratch / "new-grant.json"
    write_json(
        grant,
        {
            "version": 1,
            "profile_id": "cleanroom-profile",
            "username": "cleanroom",
            "identity": identity,
            "scopes": list(MCP_DELEGATE_SCOPES),
            "servers": ["http://127.0.0.1:1"],
        },
    )
    requests = (
        "\n".join(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "cleanroom", "version": "1"},
                        },
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            )
        )
        + "\n"
    )
    runtime = installed / "runtime" / ("bin/python3" if args.macos else "python.exe")
    result = subprocess.run(
        [
            str(runtime),
            "-X",
            "utf8",
            "-m",
            "agent_harness.mcp_adapter",
            "--grant",
            str(grant),
        ],
        input=requests,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
    )
    if result.returncode:
        raise RuntimeError("fresh MCP failed: " + result.stderr[-500:])
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    tools = next(item for item in responses if item.get("id") == 2)["result"]["tools"]
    if len(tools) != 6:
        raise RuntimeError("fresh MCP tool catalog is incomplete")
    print(
        json.dumps(
            {
                "version": args.expected_version,
                "compiled_modules": len(list(installed_package.glob("*.py"))),
                "mcp_tools": len(tools),
                "host_data": str(bridge._host_state_root()),
                "installed_bundle": installed.name,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
