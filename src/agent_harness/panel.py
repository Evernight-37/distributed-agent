"""Authenticated project workgroups; standard-library HTTP service."""

import argparse
import json
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from collaboration_store import Store, digest
from coordinator import Coordinator
from runtime_paths import ASSET_ROOT, DEFAULT_STATE
from broker import PullBackend
from common import lock, read_json


def action(store, user, data, backend):
    who, op = user["id"], data["action"]
    if op == "create-project":
        return store.project_create(who, data["name"])
    if op == "invite":
        return store.invite(who, data["project"])
    if op == "join-project":
        return store.join(who, data["invite"])
    if op == "enroll-device":
        return backend.create_pair(who, data["name"])
    if op == "add-agent":
        if isinstance(backend, PullBackend):
            raise PermissionError("请在这台机器的本地控制台接入 agent")
        return store.add_agent(who, data, backend)
    if op == "set-admin":
        return store.set_admin(who, data["project"], data["agent"]) or {"updated": True}
    if op == "configure-agent":
        if isinstance(backend, PullBackend):
            raise PermissionError("模型与资源设置由机器主人在本地控制台修改")
        return store.configure_agent(who, data, backend)
    if op == "message":
        return store.message(
            who,
            data["project"],
            data["body"],
            data.get("recipient_id"),
            data.get("mention_agent_id"),
            data.get("conversation_id"),
            data.get("client_message_id"),
        )
    if op == "conversation-read":
        return store.mark_conversation_read(who, data["project"], data["conversation_id"])
    if op == "memory-note-add":
        from project_memory import ProjectMemory

        return ProjectMemory(store).add_note(who, data["project"], data["body"])
    if op == "memory-note-delete":
        from project_memory import ProjectMemory

        return ProjectMemory(store).delete_note(who, data["project"], data.get("id", data.get("note_id")))
    if op == "task":
        return store.task(who, data)
    if op == "pause-project":
        with store.connect() as db:
            project = store.require_member(db, data["project"], who)
            if project["owner_id"] != who:
                raise PermissionError("只有群主可以暂停或恢复协作")
            if not isinstance(data["paused"], bool):
                raise ValueError("paused 必须为布尔值")
            db.execute("UPDATE projects SET paused=? WHERE id=?", (int(data["paused"]), project["id"]))
            store.log(db, who, op, {"project": project["id"]})
        return {"updated": True}
    if op == "refresh-task":
        with store.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (data["task"],)).fetchone()
            if not row:
                raise ValueError("未知任务")
            store.require_member(db, row["project_id"], who)
            if not store.task_visible_to_human(db, row, who):
                raise PermissionError("无权查看此任务")
            return dict(row)
    if op == "cancel-task":
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (data["task"],)).fetchone()
            if not row:
                raise ValueError("未知任务")
            store.require_agent_owner(db, row["agent_id"], who)
            if row["state"] == "queued":
                db.execute("UPDATE tasks SET state='cancelled',processed=1 WHERE id=?", (row["id"],))
                return {"status": "cancelled"}
            if row["state"] not in {"starting", "running", "unknown"}:
                return {"status": row["state"]}
            db.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (row["id"],))
            store.log(db, who, op, {"task": row["id"]})
        return {"status": "cancellation_requested"}
    if op == "templates":
        with store.connect() as db:
            agent = store.require_agent_owner(db, data["agent"], who)
        return backend.for_node(agent["node"]).rpc(
            {"action": "execution_templates", "project": agent["backend_project"], "templates": data["templates"]}
        )
    if op in {"execution-approve", "execution-cancel"}:
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM executions WHERE id=?", (data["execution"],)).fetchone()
            if not row:
                raise ValueError("未知后台作业")
            agent = store.require_agent_owner(db, row["agent_id"], who)
            if op == "execution-approve":
                if row["state"] != "awaiting_owner":
                    raise ValueError("作业不在待批准状态")
                db.execute("UPDATE executions SET state='approved' WHERE id=?", (row["id"],))
                store.log(db, who, op, {"execution": row["id"]})
                return {"status": "approved"}
            if row["state"] in {"awaiting_owner", "approved"}:
                db.execute("UPDATE executions SET state='cancelled',notified=1 WHERE id=?", (row["id"],))
                return {"status": "cancelled"}
            if row["state"] not in {"starting", "running", "unknown"}:
                return {"status": row["state"]}
            db.execute("UPDATE executions SET cancel_requested=1 WHERE id=?", (row["id"],))
            store.log(db, who, op, {"execution": row["id"]})
        return {"status": "cancellation_requested"}
    raise ValueError("未知操作")


def handler_for(root, backend=None, public_origin=None):
    root = Path(root)
    store = Store(root)
    backend = backend or PullBackend(store)
    from account_backend import AccountBackend

    accounts = AccountBackend(store, backend, action)
    attempts, attempt_lock = defaultdict(deque), threading.Lock()

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

        def session(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                pass
            token = cookie["harness_session"].value if "harness_session" in cookie else None
            self.current = store.session(token)
            return self.current

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
            if getattr(self, "current", None):
                secure = "; Secure" if self.origin().startswith("https://") else ""
                self.send_header(
                    "Set-Cookie",
                    "harness_session="
                    + self.current[0]
                    + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400"
                    + secure,
                )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {"error": "Host/site rejected"})
            try:
                path = urlsplit(self.path)
                token, csrf, user = self.session()
                if path.path == "/":
                    return self.send(200, (ASSET_ROOT / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
                if path.path == "/guide":
                    return self.send(200, (ASSET_ROOT / "web" / "guide.html").read_bytes(), "text/html; charset=utf-8")
                if path.path == "/api/session":
                    return self.send(
                        200, {"csrf": csrf, "user": user, "bootstrap_required": store.bootstrap_required()}
                    )
                if not user:
                    return self.send(401, {"error": "请先登录"})
                if path.path == "/api/snapshot":
                    snapshot = store.snapshot(user["id"])
                    if hasattr(backend, "snapshot_nodes"):
                        snapshot["nodes"] = backend.snapshot_nodes(user["id"])
                    return self.send(200, {**snapshot, "user": user})
                if path.path == "/api/project":
                    return self.send(200, store.project_view(user["id"], parse_qs(path.query).get("id", [""])[0]))
                return self.send(404, {"error": "Not found"})
            except PermissionError as exc:
                self.send(403, {"error": str(exc)})
            except (ValueError, KeyError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception:
                self.send(500, {"error": "后台操作失败，请检查服务日志"})

        def do_POST(self):
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
            if not self.allowed() or self.headers.get("Origin") != self.origin():
                return self.send(403, {"error": "Origin rejected"})
            try:
                token, csrf, user = self.session()
                if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf):
                    raise PermissionError("CSRF rejected")
                if (
                    self.path != "/api/action"
                    or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                ):
                    raise ValueError("Expected JSON action")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 262144:
                    raise ValueError("Invalid body size")
                data = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Expected object")
                op = data["action"]
                if op in {"login", "bootstrap", "register-user"}:
                    with attempt_lock:
                        bucket, now = attempts[self.client_address[0]], time.monotonic()
                        while bucket and bucket[0] < now - 60:
                            bucket.popleft()
                        if len(bucket) >= 10:
                            return self.send(429, {"error": "登录操作过于频繁，请稍后重试"})
                        bucket.append(now)
                    if op != "login":
                        store.signup(
                            data["username"], data["password"], bootstrap=op == "bootstrap", invite=data.get("invite")
                        )
                    self.current = store.authenticate(data["username"], data["password"], token)
                    return self.send(200, {"user": self.current[2], "csrf": self.current[1]})
                if not user:
                    return self.send(401, {"error": "请先登录"})
                if op == "logout":
                    with store.connect() as db:
                        db.execute("DELETE FROM sessions WHERE id=?", (digest(token),))
                    self.current = store.session()
                    return self.send(200, {"user": None, "csrf": self.current[1]})
                return self.send(200, action(store, user, data, backend))
            except PermissionError as exc:
                self.send(403, {"error": str(exc)})
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception:
                self.send(500, {"error": "后台操作失败，请检查服务日志"})

    Handler.store = store
    Handler.backend = backend
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--origin", help="HTTPS reverse-proxy origin; backend remains loopback-only")
    parser.add_argument(
        "--desktop-control", action="store_true", help="Enable owner-only local application lifecycle control"
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
            from host_control import managed_handler

            server.RequestHandlerClass = managed_handler(handler, server, Path(args.state) / "desktop-host.json")
        try:
            server.serve_forever()
        finally:
            coordinator.stop_event.set()
            server.server_close()


if __name__ == "__main__":
    main()
