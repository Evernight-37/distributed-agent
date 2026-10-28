"""Hidden local receiver. No model calls until the owner enables a binding."""

import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_harness.common import (
    lock,
    read_json,
    write_json,
)
from agent_harness.pull_worker import Connector, enroll
from agent_harness.worker import ManagedRuntime


def receiver_handler(port):
    """Return the minimal loopback base handler used by the Desktop API."""
    expected_host = f"127.0.0.1:{port}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value, status=200, content_type="application/json"):
            import json

            body = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") == expected_host

        def do_GET(self):
            self.respond({"error": "Not found"}, 404)

        def do_POST(self):
            self.respond({"error": "Not found"}, 404)

    return Handler


def receiver_runtime(root):
    """Bind the installed worker implementation to one receiver state root."""
    root = Path(root).resolve()
    if not (root / "worker.py").is_file():
        raise RuntimeError("Receiver worker runtime is missing")
    # desktop_service and ManagedRuntime come from the same installed bundle;
    # deploy_runtime copies that exact bundle to root before this process is
    # started.  Only mutable state is rooted at the enrolled receiver path.
    return ManagedRuntime(root)


def windows_user_sid():
    # whoami uses a Windows code page, even when Python runs with -X utf8.
    # Only the final ASCII SID field is needed; never decode the username.
    import subprocess

    output = subprocess.check_output(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    match = re.search(rb'"(S-1-[0-9]+(?:-[0-9]+)+)"\s*$', output)
    if not match:
        raise RuntimeError("无法确认当前 Windows 用户身份，接收服务未启动")
    return match.group(1).decode("ascii")


def serve(root):
    root = Path(root).resolve()
    runtime = receiver_runtime(root)
    connector = Connector(root / "pull-config.json", executor=runtime.rpc)
    stop = threading.Event()
    key, instance = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    # Share the existing lock with CLI receivers. Never run two receivers.
    with lock(root / "pull-worker.lock"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), receiver_handler(0))
        port = server.server_address[1]
        base = receiver_handler(port)
        origin = f"http://127.0.0.1:{port}"

        class Handler(base):
            def desktop(self):
                if not self.valid_host() or not hmac.compare_digest(self.headers.get("X-Desktop-Key", ""), key):
                    self.respond({"ok": False, "error": "Local app authentication required"}, 403)
                    return
                try:
                    route = self.path.removeprefix("/desktop/")
                    if self.command == "POST":
                        if self.headers.get("Origin") != origin:
                            raise ValueError("Local Origin required")
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 0 < size <= 65536:
                            raise ValueError("Invalid request size")
                        data = json.loads(self.rfile.read(size))
                    else:
                        data = None
                    if route == "status" and data is None:
                        state = read_json(root / "connector-status.json", {})
                        result = {
                            "running": True,
                            "stopping": stop.is_set(),
                            "online": bool(state.get("online")) and time.time() - state.get("updated", 0) < 20,
                        }
                    elif route == "action" and data is not None:
                        action = data.pop("action")
                        if action == "approve-execution":
                            result = connector.approve(
                                data["job_id"],
                                data["approved"],
                                data.get("expected_template"),
                            ) or {"updated": True}
                        else:
                            raise ValueError("Unknown desktop action")
                    elif route == "stop" and data is not None:
                        stop.set()
                        result = {"stopping": True}
                    else:
                        raise ValueError("Unknown desktop operation")
                    self.respond({"ok": True, "instance": instance, "result": result})
                except Exception as exc:
                    self.respond({"ok": False, "error": str(exc)}, 400)

            def do_GET(self):
                if self.path.startswith("/desktop/"):
                    return self.desktop()
                return self.respond({"error": "Not found"}, 404)

            def do_POST(self):
                if self.path.startswith("/desktop/"):
                    return self.desktop()
                return self.respond({"error": "Not found"}, 404)

        server.RequestHandlerClass = Handler
        write_json(
            root / "desktop-service.json",
            {
                "url": origin,
                "key": key,
                "instance": instance,
                "pid": os.getpid(),
                "assets": str(Path(__file__).resolve().parent),
            },
        )
        if os.name != "nt":
            (root / "desktop-service.json").chmod(0o600)
        else:
            # Use the same ACL routine as enrollment without exposing the key.
            import subprocess

            sid = windows_user_sid()
            result = subprocess.run(
                [
                    "icacls.exe",
                    str(root / "desktop-service.json"),
                    "/inheritance:r",
                    "/grant:r",
                    "*" + sid + ":F",
                    "*S-1-5-18:F",
                ],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode:
                raise RuntimeError("本机服务权限设置失败")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            while not stop.is_set():
                try:
                    # Keep the desktop control plane responsive to stop/restart.
                    # CLI and systemd receivers may use the full 10-second wait,
                    # but the in-process stop Event cannot cancel urllib mid-call.
                    connector.cycle(wait_seconds=1)
                except Exception as exc:
                    write_json(
                        root / "connector-status.json",
                        {
                            "online": False,
                            "error": type(exc).__name__,
                            "updated": time.time(),
                        },
                    )
                else:
                    write_json(
                        root / "connector-status.json",
                        {"online": True, "updated": time.time()},
                    )
                stop.wait(0.25)
        finally:
            retire = getattr(runtime, "retire_idle_workers", None)
            if retire:
                retire()
            server.shutdown()
            server.server_close()
            write_json(
                root / "connector-status.json",
                {"online": False, "updated": time.time()},
            )


if __name__ == "__main__":
    command, root = sys.argv[1:]
    if command == "pair":
        try:
            data = json.load(sys.stdin)
            value = enroll(
                data["server"],
                data["code"],
                root,
                broker_proxy=data.get("broker_proxy"),
            )
            print(json.dumps({"ok": True, "result": value}, ensure_ascii=False))
        except Exception as exc:
            error = str(exc)
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    error = json.load(exc).get("error", "服务拒绝配对，请重新生成配对码")
                except (ValueError, AttributeError):
                    pass
            print(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
    elif command == "serve":
        serve(root)
    else:
        raise ValueError("Unknown service command")
