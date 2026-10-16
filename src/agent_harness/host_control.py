"""Capability-protected local control for an app-managed workbench host."""

import hmac
import os
import secrets
import threading
from pathlib import Path
from common import write_json


def protect(path):
    if os.name != "nt":
        Path(path).chmod(0o600)
        return
    import subprocess
    from desktop_service import windows_user_sid

    result = subprocess.run(
        ["icacls.exe", str(path), "/inheritance:r", "/grant:r", "*" + windows_user_sid() + ":F", "*S-1-5-18:F"],
        capture_output=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("本机服务权限设置失败")


def managed_handler(base, server, registry):
    key, instance = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    origin = "http://127.0.0.1:" + str(server.server_port)
    stopping = threading.Event()

    class Handler(base):
        def control(self):
            if (
                self.client_address[0] != "127.0.0.1"
                or self.headers.get("Host") != origin.removeprefix("http://")
                or not hmac.compare_digest(self.headers.get("X-Desktop-Key", ""), key)
            ):
                self.send(403, {"error": "Local app authentication required"})
                return
            if self.command == "GET" and self.path == "/desktop/status":
                return self.send(
                    200,
                    {
                        "ok": True,
                        "instance": instance,
                        "result": {"running": True, "stopping": stopping.is_set(), "managed": True},
                    },
                )
            if self.command != "POST" or self.path != "/desktop/stop" or self.headers.get("Origin") != origin:
                self.send(403, {"error": "Local operation rejected"})
                return
            if self.headers.get("Content-Length") != "2" or self.rfile.read(2) != b"{}":
                self.send(400, {"error": "Invalid stop request"})
                return
            stopping.set()
            self.send(200, {"ok": True, "instance": instance, "result": {"stopping": True}})
            threading.Thread(target=server.shutdown, daemon=True).start()

        def do_GET(self):
            if self.path.startswith("/desktop/"):
                return self.control()
            return super().do_GET()

        def do_POST(self):
            if self.path.startswith("/desktop/"):
                return self.control()
            return super().do_POST()

    write_json(
        registry,
        {
            "url": origin,
            "key": key,
            "instance": instance,
            "pid": os.getpid(),
            "assets": str(Path(__file__).resolve().parent),
        },
    )
    protect(registry)
    return Handler
