"""Signed account/device collaboration service; standard-library HTTP transport."""

import argparse
import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from agent_harness.collaboration_store import Store
from agent_harness.coordinator import Coordinator
from agent_harness.runtime_paths import ASSET_ROOT, DEFAULT_STATE
from agent_harness.broker import PullBackend
from agent_harness.common import lock, read_json
from agent_harness.protocol import hub_advertisement


RETIRED_BROWSER_APIS = frozenset(
    {
        "/api/session",
        "/api/events",
        "/api/health",
        "/api/snapshot",
        "/api/project",
        "/api/action",
    }
)


def handler_for(root, backend=None, public_origin=None):
    root = Path(root)
    store = Store(root)
    backend = backend or PullBackend(store)
    from agent_harness.account_backend import AccountBackend

    accounts = AccountBackend(store, backend)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass

        def public_origin(self):
            candidate = public_origin
            if not candidate:
                setting = read_json(root / "team-service.json", {})
                if (
                    setting.get("mode") != "tailscale-serve"
                    or setting.get("proxy") != "http://127.0.0.1:8765"
                    or setting.get("tailnet_only") is not True
                ):
                    return None
                candidate = setting.get("url")
            if not isinstance(candidate, str):
                return None
            parsed = urlsplit(candidate)
            if parsed.scheme != "https" or parsed.path or parsed.query or parsed.fragment or parsed.username:
                return None
            if not parsed.hostname or not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net", parsed.hostname.lower()):
                return None
            return "https://" + parsed.netloc

        def origin(self):
            public = self.public_origin()
            return (
                public
                if public and self.headers.get("Host") == urlsplit(public).netloc
                else f"http://127.0.0.1:{self.server.server_port}"
            )

        def allowed(self):
            local = f"127.0.0.1:{self.server.server_port}"
            public = self.public_origin()
            allowed = {local}
            if public:
                allowed.add(urlsplit(public).netloc)
            return self.headers.get("Host") in allowed and self.headers.get("Sec-Fetch-Site") != "cross-site"

        def send(self, status, data, content_type="application/json; charset=utf-8"):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {"error": "Host/site rejected"})
            try:
                request_path = urlsplit(self.path).path
                if request_path == "/":
                    return self.send(
                        200,
                        (ASSET_ROOT / "web" / "index.html").read_bytes(),
                        "text/html; charset=utf-8",
                    )
                if request_path == "/guide":
                    return self.send(
                        200,
                        (ASSET_ROOT / "web" / "guide.html").read_bytes(),
                        "text/html; charset=utf-8",
                    )
                if request_path == "/healthz":
                    return self.send(
                        200,
                        {"ok": True, "service": "agent-harness-hub", "protocol": hub_advertisement()},
                    )
                if request_path in RETIRED_BROWSER_APIS:
                    return self.send(410, {"error": "浏览器工作台已停用；请使用 Agent Harness 应用或 Codex MCP"})
                return self.send(404, {"error": "Not found"})
            except PermissionError as exc:
                self.send(403, {"error": str(exc)})
            except (ValueError, KeyError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception:
                self.send(500, {"error": "后台操作失败，请检查服务日志"})

        def do_POST(self):
            if not self.allowed():
                return self.send(403, {"error": "Host/site rejected"})
            if self.path == "/api/account":
                try:
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError("Expected JSON")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 8 * 1024 * 1024:
                        raise ValueError("Invalid body size")
                    data = json.loads(self.rfile.read(size))
                    return self.send(200, accounts.api(data))
                except PermissionError as exc:
                    return self.send(403, {"error": str(exc)})
                except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                    return self.send(400, {"error": str(exc)})
                except Exception:
                    return self.send(500, {"error": "Account operation failed"})
            if self.path.startswith("/api/device/"):
                try:
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError("Expected JSON")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 524288:
                        raise ValueError("Invalid body size")
                    data = json.loads(self.rfile.read(size).decode("utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("Expected object")
                    auth = self.headers.get("Authorization", "")
                    bearer = auth[7:] if auth.startswith("Bearer ") else None
                    return self.send(200, backend.device_api(self.path, data, bearer))
                except PermissionError as exc:
                    return self.send(403, {"error": str(exc)})
                except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                    return self.send(400, {"error": str(exc)})
                except Exception:
                    return self.send(500, {"error": "Device operation failed"})
            if urlsplit(self.path).path in RETIRED_BROWSER_APIS:
                return self.send(410, {"error": "浏览器工作台已停用；请使用 Agent Harness 应用或 Codex MCP"})
            return self.send(404, {"error": "Not found"})

    Handler.store = store
    Handler.backend = backend
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--origin", help="HTTPS reverse-proxy origin; backend remains loopback-only")
    parser.add_argument(
        "--desktop-control",
        action="store_true",
        help="Enable owner-only local application lifecycle control",
    )
    args = parser.parse_args()
    if args.origin:
        parsed = urlsplit(args.origin)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            parser.error("--origin requires an HTTPS origin without path or credentials")
    with lock(Path(args.state) / "coordinator.lock"):
        handler = handler_for(args.state, public_origin=args.origin)
        coordinator = Coordinator(handler.store, handler.backend)
        threading.Thread(target=coordinator.run, daemon=True).start()
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
        if args.desktop_control:
            from agent_harness.host_control import managed_handler

            server.RequestHandlerClass = managed_handler(handler, server, Path(args.state) / "desktop-host.json")
        try:
            server.serve_forever()
        finally:
            coordinator.stop_event.set()
            server.server_close()


if __name__ == "__main__":
    main()
