"""Verify first-use local registration using the packaged frozen-app paths."""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--runtime-probe", action="store_true")
    args = parser.parse_args()
    bundle = args.app.resolve() / "Contents/Resources/service-bundle"
    if not args.runtime_probe:
        runtime = bundle / "runtime/bin/python3"
        result = subprocess.run(
            [str(runtime), "-B", str(Path(__file__).resolve()), str(args.app.resolve()), "--runtime-probe"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if result.returncode:
            raise RuntimeError("packaged account probe failed: " + result.stderr[-1000:])
        print(result.stdout.strip())
        return
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    from agent_harness.app_bridge import AppBridge
    from agent_harness.app_lifecycle import Lifecycle
    from agent_harness import service_install

    # Never attach this test to an existing real collaboration service.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8765))
    service_install._installed = bundle
    sys.frozen = True
    with tempfile.TemporaryDirectory(prefix="harness-account-check-", dir="/private/tmp") as directory:
        bridge = AppBridge(Path(directory) / "data")
        try:
            result = bridge.invoke("register", {"username": "macOS isolated test", "password": "test-only"})
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "Registration failed"))
            assert result["result"]["created"]
            state = bridge._host_state_root()
            assert state.stat().st_mode & 0o777 == 0o700
            assert (state / "collaboration.sqlite3").is_file()
            # Repeated access must not remove the directory's traversal bit.
            bridge._server("http://127.0.0.1:8765")
            dashboard = bridge.invoke("dashboard")
            if not dashboard.get("ok"):
                raise RuntimeError(dashboard.get("error", "Dashboard failed"))
            assert state.stat().st_mode & 0o777 == 0o700
            print(
                json.dumps(
                    {
                        "registered": True,
                        "dashboard": True,
                        "private_directory_mode": "700",
                    }
                )
            )
        finally:
            Lifecycle(bridge.root, host_root=bridge._host_state_root()).stop()


if __name__ == "__main__":
    main()
