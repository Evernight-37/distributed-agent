"""Verify first-use local registration using the packaged frozen-app paths."""

import argparse
import json
import os
import socket
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    bundle = args.app.resolve() / "Contents/Resources/service-bundle"
    sys.path.insert(0, str(bundle / "client"))
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    from app_bridge import AppBridge
    from app_lifecycle import Lifecycle
    import service_install

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
            print(json.dumps({"registered": True, "dashboard": True, "private_directory_mode": "700"}))
        finally:
            Lifecycle(bridge.root, host_root=bridge._host_state_root()).stop()


if __name__ == "__main__":
    main()
