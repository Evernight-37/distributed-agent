"""Proof-of-possession account API; the service never receives local passwords."""

import hashlib
import json
import secrets
import time
from agent_harness.account_identity import (
    account_id,
    decode,
    verify_envelope,
    canonical,
)
from agent_harness.collaboration_store import digest
from agent_harness.owner_backend import OwnerBackend
from agent_harness.owner_worker import execution_project
from agent_harness.protocol import hub_advertisement
from agent_harness.hub_api import HubAccountAPI
from agent_harness.health import HealthCode, safe_message
from agent_harness.common import read_json


MAX_ACTIVE_CHALLENGES = 4096
MAX_PENDING_DEVICE_REQUESTS = 1000
MAX_PENDING_REQUESTS_PER_KEY = 5


def device_node(public):
    return "device-" + hashlib.sha256(public.encode()).hexdigest()[:24]


class AccountBackend:
    def __init__(self, store, backend):
        self.store, self.backend = store, backend
        self.owners = OwnerBackend(store)
        self.account_api = HubAccountAPI(self._api)

    @staticmethod
    def _cleanup_ephemeral(db, now=None):
        now = time.time() if now is None else now
        db.execute("DELETE FROM account_challenges WHERE expires<?", (now,))
        db.execute("DELETE FROM account_requests WHERE expires<?", (now,))

    def api(self, data):
        return self.account_api.dispatch(data)

    def _api(self, data):
        if not isinstance(data, dict):
            raise ValueError("Expected account request")
        action = data.get("action")
        if action == "challenge":
            public = data["public_key"]
            decode(public, 32)
            nonce = secrets.token_urlsafe(32)
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._cleanup_ephemeral(db)
                if db.execute("SELECT COUNT(*) FROM account_challenges").fetchone()[0] >= MAX_ACTIVE_CHALLENGES:
                    raise RuntimeError("服务当前关联请求过多，请稍后重试")
                db.execute(
                    "INSERT INTO account_challenges VALUES(?,?,?)",
                    (nonce, public, time.time() + 60),
                )
            return {
                "nonce": nonce,
                "expires_in": 60,
                "service_id": self.store.service_id(),
                "protocol": hub_advertisement(),
                "features": [
                    "mention_agent_id",
                    "anchored_reply_draft",
                    "conversation_privacy_v2",
                    "service_host_owner_v1",
                    "codex_mcp_v1",
                    "employee_delete_v1",
                    "employee_delete_preview_v1",
                    "chat_attachments_v1",
                    "device_file_transfers_v1",
                    "event_cursor_v1",
                    "health_v1",
                    "account_recovery_v1",
                ],
            }
        if action == "request-device":
            public = data["public_key"]
            decode(public, 32)
            name = self._name(data.get("name", "新电脑"))
            code = secrets.token_urlsafe(24)
            rid = "request-" + secrets.token_hex(16)
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._cleanup_ephemeral(db)
                if db.execute("SELECT COUNT(*) FROM account_requests").fetchone()[0] >= MAX_PENDING_DEVICE_REQUESTS:
                    raise RuntimeError("服务当前设备关联请求过多，请稍后重试")
                if (
                    db.execute(
                        "SELECT COUNT(*) FROM account_requests WHERE public_key=?",
                        (public,),
                    ).fetchone()[0]
                    >= MAX_PENDING_REQUESTS_PER_KEY
                ):
                    raise RuntimeError("这台设备已有过多待处理关联请求，请等待过期后重试")
                db.execute(
                    "INSERT INTO account_requests(id,code_hash,public_key,name,expires) VALUES(?,?,?,?,?)",
                    (rid, digest(code), public, name, time.time() + 600),
                )
            return {"request_id": rid, "code": code, "expires_in": 600}
        if action == "claim-device":
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM account_requests WHERE id=? AND code_hash=? AND expires>? AND claimed=0",
                    (data["request_id"], digest(data["code"]), time.time()),
                ).fetchone()
                if not row:
                    raise PermissionError("Device request unavailable")
                if not row["account_id"]:
                    return {"state": "pending", "status": "pending"}
                device = db.execute(
                    "SELECT revoked FROM account_devices WHERE public_key=?",
                    (row["public_key"],),
                ).fetchone()
                if not device or device["revoked"]:
                    raise PermissionError("Device approval revoked")
                db.execute("UPDATE account_requests SET claimed=1 WHERE id=?", (row["id"],))
                return {
                    "state": "approved",
                    "status": "approved",
                    "account_id": row["account_id"],
                    "username": self._username(db, row["account_id"]),
                    "node": device_node(row["public_key"]),
                    "public_key": row["public_key"],
                    "chain": json.loads(row["chain"]),
                }
        if action not in {
            "register",
            "rpc",
            "accept-project-invite",
            "activate-device",
        }:
            raise ValueError("Unknown account operation")
        verified = verify_envelope(data["envelope"])
        body = verified["body"]
        public = verified["public_key"]
        user = verified["account_id"]
        if body.get("domain") != "harness-account-request-v1" or not isinstance(body.get("payload"), dict):
            raise PermissionError("Wrong request domain")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            challenge = db.execute(
                "SELECT * FROM account_challenges WHERE nonce=? AND public_key=? AND expires>?",
                (body.get("nonce"), public, time.time()),
            ).fetchone()
            if not challenge:
                raise PermissionError("Challenge expired or already used")
            db.execute("DELETE FROM account_challenges WHERE nonce=?", (body["nonce"],))
        # Consume even rejected authenticated operations to prevent replay after state changes.
        if action == "register":
            if verified.get("delegate_scopes") is not None:
                raise PermissionError("A delegate cannot register an account")
            if body.get("operation") != "register":
                raise PermissionError("Wrong registration proof")
            name = self._name(body["payload"].get("name"))
            device_name = self._name(body["payload"].get("device_name", "我的电脑"))
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT 1 FROM users WHERE id=?", (user,)).fetchone():
                    db.execute(
                        "INSERT INTO users(id,username) VALUES(?,?)",
                        (user, name + " · " + user[-8:]),
                    )
                self._register_chain(db, verified, data["envelope"].get("chain", []), device_name)
            return {
                "account_id": user,
                "name": name,
                "username": name,
                "node": device_node(public),
                "public_key": public,
            }
        if action == "activate-device":
            if body.get("operation") != "activate-device":
                raise PermissionError("Wrong device proof")
            device_name = self._name(body["payload"].get("device_name", "我的电脑"))
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT 1 FROM users WHERE id=?", (user,)).fetchone():
                    raise PermissionError("此身份尚未加入该服务的项目")
                self._register_chain(db, verified, data["envelope"].get("chain", []), device_name)
                username = self._username(db, user)
            return {
                "account_id": user,
                "node": device_node(public),
                "username": username,
            }
        if action == "accept-project-invite":
            if verified.get("delegate_scopes") is not None:
                raise PermissionError("A delegate cannot accept project invitations")
            if body.get("operation") != "accept-project-invite":
                raise PermissionError("Wrong invite proof")
            payload = body["payload"]
            name = self._name(payload.get("name"))
            device_name = self._name(payload.get("device_name", "我的电脑"))
            token = payload.get("invite")
            if not isinstance(token, str) or len(token) > 256:
                raise PermissionError("邀请无效或已使用")
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                invitation = db.execute(
                    "SELECT * FROM invitations WHERE hash=? AND used=0 AND expires>?",
                    (digest(token), time.time()),
                ).fetchone()
                if not invitation:
                    raise PermissionError("邀请无效或已使用")
                if not db.execute("SELECT 1 FROM users WHERE id=?", (user,)).fetchone():
                    db.execute(
                        "INSERT INTO users(id,username) VALUES(?,?)",
                        (user, name + " · " + user[-8:]),
                    )
                self._register_chain(db, verified, data["envelope"].get("chain", []), device_name)
                db.execute(
                    "INSERT OR IGNORE INTO project_users VALUES(?,?)",
                    (invitation["project_id"], user),
                )
                db.execute("UPDATE invitations SET used=1 WHERE hash=?", (digest(token),))
                self.store.log(
                    db,
                    user,
                    "accept_project_invite",
                    {"project": invitation["project_id"]},
                )
                self.store.emit_project_event(db, invitation["project_id"], "membership")
                return {
                    "project": invitation["project_id"],
                    "account_id": user,
                    "node": device_node(public),
                }
        with self.store.connect() as db:
            delegate_scopes = verified.get("delegate_scopes")
            device_keys = (
                set(verified["chain_keys"]) - {public} if delegate_scopes is not None else set(verified["chain_keys"])
            )
            for key in device_keys:
                device = db.execute(
                    "SELECT * FROM account_devices WHERE public_key=? AND account_id=?",
                    (key, user),
                ).fetchone()
                if not device or device["revoked"]:
                    raise PermissionError("Device is not authorized")
            if delegate_scopes is not None:
                delegate = db.execute(
                    "SELECT * FROM account_delegates WHERE public_key=? AND account_id=?",
                    (public, user),
                ).fetchone()
                if (
                    not delegate
                    or delegate["revoked"]
                    or tuple(json.loads(delegate["scopes"])) != tuple(delegate_scopes)
                ):
                    raise PermissionError("Delegate is not authorized")
                required = {
                    "service-links": "collaboration.read",
                    "dashboard": "collaboration.read",
                    "project": "collaboration.read",
                    "events-wait": "collaboration.read",
                    "health-status": "collaboration.read",
                    "message": "collaboration.message",
                    "conversation-read": "collaboration.read",
                    "task": "collaboration.task",
                    "task-status": "collaboration.read",
                }.get(body["operation"])
                if not required or required not in delegate_scopes:
                    raise PermissionError("Delegate scope does not allow this operation")
        return self._operation(user, public, body["operation"], body["payload"], delegate_scopes)

    def _name(self, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError("Name must contain 1–120 characters")
        return name.strip()

    def _register_chain(self, db, verified, chain, device_name):
        user, public = verified["account_id"], verified["public_key"]
        delegate_scopes = verified.get("delegate_scopes")
        device_chain = chain[:-1] if delegate_scopes is not None else chain
        keys = [verified["root_public_key"]] + [cert["body"]["child"] for cert in device_chain]
        for index, key in enumerate(keys):
            row = db.execute(
                "SELECT account_id,revoked FROM account_devices WHERE public_key=?",
                (key,),
            ).fetchone()
            if row and (row["account_id"] != user or row["revoked"]):
                raise PermissionError("设备授权已撤销或属于其他账户")
            if not row:
                db.execute(
                    "INSERT INTO account_devices VALUES(?,?,?,?,0)",
                    (
                        key,
                        user,
                        device_name if key == public else "关联设备",
                        json.dumps(device_chain[:index]),
                    ),
                )
            self._node(db, key, user)
        if delegate_scopes is not None:
            row = db.execute(
                "SELECT account_id,revoked,scopes FROM account_delegates WHERE public_key=?",
                (public,),
            ).fetchone()
            encoded = json.dumps(list(delegate_scopes), ensure_ascii=False)
            if row and (row["account_id"] != user or row["revoked"] or row["scopes"] != encoded):
                raise PermissionError("Codex delegation was revoked or changed")
            if not row:
                db.execute(
                    "INSERT INTO account_delegates VALUES(?,?,?,?,?,0)",
                    (public, user, device_name, encoded, json.dumps(chain)),
                )

    def _operation(self, user, public, op, p, delegate_scopes=None):
        if op == "health-status":
            return self.store.health_status()
        if op == "events-wait":
            return self.store.events_wait(
                user,
                p.get("cursor", 0),
                p.get("wait_seconds", 0),
            )
        if op == "service-links":
            with self.store.connect() as db:
                return {
                    "servers": [
                        row["server"]
                        for row in db.execute(
                            "SELECT server FROM account_services WHERE account_id=? ORDER BY joined_at",
                            (user,),
                        )
                    ]
                }
        if op == "link-service":
            from agent_harness.pull_worker import validate_server

            server = p["server"].rstrip("/")
            validate_server(server)
            with self.store.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO account_services VALUES(?,?,?)",
                    (user, server, time.time()),
                )
            return {"server": server}
        if op == "dashboard":
            result = self.store.snapshot(user)
            result.update(self.owners.snapshot(user))
            from agent_harness.file_transfers import FileTransfers

            result["transfers"] = FileTransfers(self.store).list(user)
            host = self.store.host_owner()
            result["hosted_by_me"] = bool(host and host["owner_account_id"] == user)
            result["host_node"] = host["host_node"] if result["hosted_by_me"] else None
            result["service_id"] = self.store.service_id()
            with self.store.connect() as db:
                result["user"] = {"id": user, "username": self._username(db, user)}
                visits = {
                    r["project_id"]: r["visited"]
                    for r in db.execute(
                        "SELECT * FROM account_project_visits WHERE account_id=?",
                        (user,),
                    )
                }
                for project in result["projects"]:
                    project["last_entered"] = visits.get(project["id"])
                devices = []
                for row in db.execute(
                    "SELECT * FROM account_devices WHERE account_id=? AND revoked=0",
                    (user,),
                ):
                    node = self._node(db, row["public_key"], user)
                    config = read_json(self.store.root / "nodes" / (node + ".json"), {})
                    seen = db.execute("SELECT last_seen FROM pull_devices WHERE node=?", (node,)).fetchone()
                    last_seen = seen["last_seen"] if seen else None
                    devices.append(
                        {
                            "id": node,
                            "node": node,
                            "name": row["name"],
                            "public_key": row["public_key"],
                            "is_root": account_id(row["public_key"]) == user,
                            "online": bool(last_seen and last_seen > time.time() - 45),
                            "last_seen": last_seen,
                            "health": config.get("health") if isinstance(config.get("health"), dict) else None,
                        }
                    )
                result["devices"] = devices
                device_map = {d["node"]: d for d in devices}
                for employee in result["employees"]:
                    settings = employee.pop("settings")
                    settings.pop("remote_path", None)
                    employee.update(settings)
                    pending = []
                    for command in db.execute(
                        "SELECT id,envelope,state FROM owner_commands WHERE owner_id=? AND node=? AND state IN ('queued','claimed')",
                        (user, employee["node"]),
                    ):
                        if json.loads(command["envelope"])["body"]["data"].get("id") == employee["id"]:
                            pending.append(command["id"])
                    for row in db.execute(
                        "SELECT envelope,state,result FROM owner_commands WHERE owner_id=? AND node=? ORDER BY created_at DESC",
                        (user, employee["node"]),
                    ):
                        if json.loads(row["envelope"])["body"]["data"].get("id") == employee["id"]:
                            if row["state"] == "failed":
                                employee["last_error"] = safe_message(
                                    json.loads(row["result"] or "{}").get("error", "操作失败"),
                                    "操作失败",
                                )
                                employee["status_code"] = HealthCode.EXECUTION_FAILED
                            break
                    employee["pending"] = pending
                    employee["online"] = device_map.get(employee["node"], {}).get("online", False)
                    employee["health"] = device_map.get(employee["node"], {}).get("health")
                    health = employee["health"]
                    health_code = None
                    if isinstance(health, dict):
                        components = health.get("components")
                        if isinstance(components, list):
                            health_code = next(
                                (
                                    item.get("code")
                                    for item in components
                                    if isinstance(item, dict) and item.get("code") not in {None, HealthCode.OK}
                                ),
                                None,
                            )
                    if pending:
                        employee["status"] = "pending"
                        employee["status_code"] = HealthCode.NOT_CONFIGURED
                    elif health_code:
                        employee["status"] = "error" if health.get("overall") == "unavailable" else "degraded"
                        employee["status_code"] = health_code
                    elif not employee["online"]:
                        employee["status"] = "offline"
                        employee["status_code"] = HealthCode.OFFLINE
                    else:
                        employee.setdefault("status_code", HealthCode.OK)
            return result
        if op == "project":
            project = p.get("project", p.get("id"))
            if p.get("active_conversation_id"):
                self.store.mark_conversation_read(user, project, p["active_conversation_id"])
            result = self.store.project_view(user, project)
            from agent_harness.project_memory import ProjectMemory

            result["project_memory"] = ProjectMemory(self.store).snapshot(user, project)
            with self.store.connect() as db:
                if p.get("visit") is True:
                    db.execute(
                        "INSERT INTO account_project_visits VALUES(?,?,?) ON CONFLICT(account_id,project_id) DO UPDATE SET visited=excluded.visited",
                        (user, project, time.time()),
                    )
                employees = db.execute(
                    "SELECT id,node,status FROM employees WHERE owner_id IN (SELECT user_id FROM project_users WHERE project_id=?)",
                    (project,),
                ).fetchall()
                dispatch_state = {}
                for command in db.execute(
                    "SELECT envelope FROM owner_commands WHERE state='applied' AND owner_id IN (SELECT user_id FROM project_users WHERE project_id=?) ORDER BY created_at,rowid",
                    (project,),
                ):
                    body = json.loads(command["envelope"])["body"]
                    data = body["data"]
                    if body["action"] in {"dispatch", "withdraw"} and data.get("project_id") == project:
                        dispatch_state[data["id"]] = body["action"]
                for member in result.get("members", []):
                    member["employee_id"] = next(
                        (
                            e["id"]
                            for e in employees
                            if e["node"] == member["node"]
                            and execution_project(e["id"], project) == member["backend_project"]
                        ),
                        None,
                    )
                    seen = db.execute(
                        "SELECT last_seen FROM pull_devices WHERE node=?",
                        (member["node"],),
                    ).fetchone()
                    member["online"] = bool(seen and seen["last_seen"] > time.time() - 45)
                result["members"] = [
                    m
                    for m in result.get("members", [])
                    if dispatch_state.get(m.get("employee_id")) != "withdraw"
                    and not any(e["id"] == m.get("employee_id") and e["status"] == "deleted" for e in employees)
                ]
                result["workspaces"] = [
                    dict(row)
                    for row in db.execute(
                        "SELECT project_id,owner_id,node,status,write_policy,updated_at "
                        "FROM project_workspaces WHERE project_id=? AND owner_id=? ORDER BY node",
                        (project, user),
                    )
                ]
            return result
        if op == "create-project":
            return self.store.project_create(user, p["name"])
        if op in {"join", "join-project"}:
            return self.store.join(user, p["invite"])
        if op == "invite":
            return self.store.invite(user, p["project"])
        if op == "message":
            return self.store.message(
                user,
                p["project"],
                p["body"],
                p.get("recipient_id"),
                p.get("mention_agent_id"),
                p.get("conversation_id"),
                p.get("client_message_id"),
                "codex" if delegate_scopes is not None else None,
                p.get("attachment_ids"),
            )
        if op in {"attachment-upload", "attachment-get"}:
            from agent_harness.attachments import Attachments

            files = Attachments(self.store)
            if op == "attachment-upload":
                return files.upload(user, p["project"], p["name"], p["base64"])
            return files.download(user, p["project"], p["id"])
        if op.startswith("transfer-"):
            if delegate_scopes is not None:
                raise PermissionError("Codex 委托身份不能操作设备文件传输")
            from agent_harness.file_transfers import FileTransfers

            files = FileTransfers(self.store)
            node = device_node(public)
            if op == "transfer-peers":
                return files.recipients(user)
            if op == "transfer-list":
                return {"transfers": files.list(user)}
            if op == "transfer-request":
                return files.request(
                    user,
                    node,
                    p["recipient_id"],
                    p.get("recipient_node"),
                    p["name"],
                    p["size"],
                    p["sha256"],
                )
            if op == "transfer-status":
                return files.status(user, p["id"])
            if op == "transfer-decide":
                return files.decide(
                    user,
                    node,
                    p["id"],
                    p["accept"],
                    recipient_node=p.get("recipient_node"),
                )
            if op == "transfer-upload-start":
                return files.upload_start(user, node, p["id"])
            if op == "transfer-upload-finish":
                return files.finish(user, node, p["id"])
            if op == "transfer-download-ticket":
                return files.download_ticket(user, node, p["id"])
            if op == "transfer-complete":
                return files.complete(user, node, p["id"])
            if op == "transfer-cancel":
                return files.cancel(user, p["id"])
            raise ValueError("Unknown file-transfer operation")
        if op == "conversation-read":
            return self.store.mark_conversation_read(user, p["project"], p["conversation_id"])
        if op == "task":
            if delegate_scopes is not None:
                p = {**p, "requested_via": "codex"}
            return self.store.task(user, p)
        if op == "task-status":
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM tasks WHERE id=?", (p.get("task"),)).fetchone()
                if not row:
                    raise ValueError("未知任务")
                self.store.require_member(db, row["project_id"], user)
                if not self.store.task_visible_to_human(db, row, user):
                    raise PermissionError("无权查看此任务")
                return dict(row)
        if op == "memory-note-add":
            from agent_harness.project_memory import ProjectMemory

            return ProjectMemory(self.store).add_note(user, p["project"], p["body"])
        if op == "memory-note-delete":
            from agent_harness.project_memory import ProjectMemory

            return ProjectMemory(self.store).delete_note(user, p["project"], p.get("id", p.get("note_id")))
        if op == "set-admin":
            if p.get("agent"):
                return self.store.set_admin(user, p["project"], p["agent"]) or {"updated": True}
            with self.store.connect() as db:
                project = self.store.require_member(db, p["project"], user)
                if project["owner_id"] != user:
                    raise PermissionError("只有群主可以指定管理员")
                db.execute(
                    "UPDATE projects SET admin_agent_id=NULL WHERE id=?",
                    (p["project"],),
                )
                self.store.emit_project_event(db, p["project"], "project")
            return {"updated": True}
        if op == "workspace-ready":
            if delegate_scopes is not None:
                raise PermissionError("Codex 委托身份不能配置本机项目目录")
            node = device_node(public)
            with self.store.connect() as db:
                self.store.require_member(db, p["project"], user)
                owned = db.execute(
                    "SELECT 1 FROM nodes WHERE name=? AND owner_id=?",
                    (node, user),
                ).fetchone()
                if not owned:
                    raise PermissionError("当前设备尚未接入此协作服务")
                db.execute(
                    "INSERT INTO project_workspaces(project_id,owner_id,node,status,write_policy,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,node) DO UPDATE SET "
                    "owner_id=excluded.owner_id,status=excluded.status,updated_at=excluded.updated_at",
                    (p["project"], user, node, "ready", "single_writer", time.time()),
                )
            return {"project": p["project"], "node": node, "status": "ready"}
        if op == "employee-delete-preview":
            with self.store.connect() as db:
                employee = db.execute(
                    "SELECT * FROM employees WHERE id=? AND owner_id=? AND status<>'deleted'",
                    (p["id"], user),
                ).fetchone()
                if not employee:
                    raise ValueError("员工不存在")
                projects = []
                active = []
                for agent in self.owners._employee_agents(db, employee["id"], user, employee["node"]):
                    project = db.execute(
                        "SELECT id,name,admin_agent_id FROM projects WHERE id=?",
                        (agent["project_id"],),
                    ).fetchone()
                    if project:
                        projects.append(
                            {
                                "id": project["id"],
                                "name": project["name"],
                                "administrator": project["admin_agent_id"] == agent["id"],
                            }
                        )
                    active.extend(
                        dict(row)
                        for row in db.execute(
                            "SELECT id,title,state FROM tasks WHERE agent_id=? AND state IN ('queued','starting','running','unknown')",
                            (agent["id"],),
                        )
                    )
                    active.extend(
                        {
                            "id": row["id"],
                            "title": row["template"],
                            "state": row["state"],
                            "kind": "execution",
                        }
                        for row in db.execute(
                            "SELECT id,template,state FROM executions WHERE agent_id=? "
                            "AND state IN ('approved','starting','running','unknown')",
                            (agent["id"],),
                        )
                    )
                return {
                    "id": employee["id"],
                    "name": employee["name"],
                    "projects": projects,
                    "active_tasks": active,
                    "can_delete": not active,
                }
        if op == "owner-command":
            verified = verify_envelope(p["envelope"], user)
            with self.store.connect() as db:
                for key in verified["chain_keys"]:
                    row = db.execute(
                        "SELECT revoked FROM account_devices WHERE public_key=? AND account_id=?",
                        (key, user),
                    ).fetchone()
                    if not row or row["revoked"]:
                        raise PermissionError("Owner command signer was revoked")
            return self.owners.enqueue(user, p["envelope"])
        if op == "command-status":
            return self.owners.command(user, p["id"])
        if op == "pair-local":
            node = device_node(public)
            if p.get("node", node) != node:
                raise PermissionError("Cannot re-pair another device")
            return self.backend.create_pair(user, node)
        if op in {"device-request", "inspect-device"}:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT id,public_key,name,account_id,expires FROM account_requests WHERE code_hash=? AND expires>? AND claimed=0",
                    (digest(p["code"]), time.time()),
                ).fetchone()
                if not row or row["account_id"]:
                    raise PermissionError("Device request unavailable")
                return dict(row, request_id=row["id"])
        if op == "approve-device":
            code = p.get("code")
            rid = p.get("request_id")
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM account_requests WHERE "
                    + ("id=?" if rid else "code_hash=?")
                    + " AND expires>? AND claimed=0",
                    (rid or digest(code or ""), time.time()),
                ).fetchone()
                if not row or row["account_id"]:
                    raise PermissionError("Device request unavailable")
                chain = p["chain"]
                # Child proof is not available to approving device. Verify certificates via
                # their parent signatures separately, using certificate helper below.
                self._verify_chain(chain, user, row["public_key"], public)
                db.execute(
                    "INSERT INTO account_devices VALUES(?,?,?,?,0)",
                    (row["public_key"], user, row["name"], json.dumps(chain)),
                )
                self._node(db, row["public_key"], user)
                db.execute(
                    "UPDATE account_requests SET account_id=?,chain=? WHERE id=?",
                    (user, json.dumps(chain), row["id"]),
                )
            return {"approved": True, "request_id": row["id"]}
        if op == "revoke-device":
            with self.store.connect() as db:
                target = p.get("public_key")
                if not target:
                    target = next(
                        (
                            r["public_key"]
                            for r in db.execute(
                                "SELECT public_key FROM account_devices WHERE account_id=?",
                                (user,),
                            )
                            if device_node(r["public_key"]) == p.get("id")
                        ),
                        None,
                    )
                row = db.execute(
                    "SELECT * FROM account_devices WHERE public_key=? AND account_id=?",
                    (target, user),
                ).fetchone()
                if not row:
                    raise PermissionError("Unknown device")
                if target == public:
                    raise ValueError("Cannot revoke the device making this request")
                if account_id(target) == user:
                    raise ValueError("Cannot revoke the root identity device; account recovery is not yet supported")
                revoked_keys = {target}
                for candidate in db.execute(
                    "SELECT public_key,chain FROM account_devices WHERE account_id=?",
                    (user,),
                ):
                    ancestors = {cert["body"]["parent"] for cert in json.loads(candidate["chain"])}
                    if target in ancestors:
                        revoked_keys.add(candidate["public_key"])
                revoked_nodes = {device_node(key) for key in revoked_keys}
                for key in revoked_keys:
                    db.execute(
                        "UPDATE account_devices SET revoked=1 WHERE public_key=?",
                        (key,),
                    )
                    node = device_node(key)
                    db.execute("DELETE FROM pull_devices WHERE node=?", (node,))
                    db.execute("UPDATE agents SET enabled=0 WHERE node=?", (node,))
                for command in db.execute(
                    "SELECT id,envelope FROM owner_commands WHERE owner_id=? AND state IN ('queued','claimed')",
                    (user,),
                ).fetchall():
                    envelope = json.loads(command["envelope"])
                    keys = [envelope.get("public_key")] + [cert["body"]["parent"] for cert in envelope.get("chain", [])]
                    if command["envelope"] and (
                        revoked_keys.intersection(keys) or envelope["body"]["target"] in revoked_nodes
                    ):
                        db.execute(
                            "UPDATE owner_commands SET state='failed',result=? WHERE id=?",
                            (
                                canonical({"error": "Device revoked"}).decode(),
                                command["id"],
                            ),
                        )
            return {"revoked": True}
        if op == "revoke-delegate":
            target = p.get("public_key")
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT * FROM account_delegates WHERE public_key=? AND account_id=?",
                    (target, user),
                ).fetchone()
                if not row:
                    return {"revoked": True, "already_absent": True}
                db.execute(
                    "UPDATE account_delegates SET revoked=1 WHERE public_key=?",
                    (target,),
                )
            return {"revoked": True}
        raise ValueError("Unsupported signed account operation")

    def _username(self, db, user):
        name = db.execute("SELECT username FROM users WHERE id=?", (user,)).fetchone()["username"]
        suffix = " · " + user[-8:]
        return name[: -len(suffix)] if name.endswith(suffix) else name

    def _node(self, db, public, user):
        node = device_node(public)
        existing = db.execute("SELECT owner_id FROM nodes WHERE name=?", (node,)).fetchone()
        if existing and existing["owner_id"] != user:
            raise PermissionError("Device node belongs to another account")
        db.execute("INSERT OR IGNORE INTO nodes VALUES(?,?)", (node, user))
        return node

    def _verify_chain(self, chain, user, child, approver):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if not isinstance(chain, list) or not 1 <= len(chain) <= 8:
            raise PermissionError("Invalid approval chain")
        parent = chain[0]["body"]["parent"]
        seen = {parent}
        if account_id(parent) != user:
            raise PermissionError("Wrong root")
        for cert in chain:
            b = cert["body"]
            if (
                set(b) != {"domain", "account_id", "parent", "child"}
                or b["domain"] != "harness-device-certificate-v1"
                or b["account_id"] != user
                or b["parent"] != parent
                or b["child"] in seen
            ):
                raise PermissionError("Invalid certificate")
            Ed25519PublicKey.from_public_bytes(decode(parent, 32)).verify(decode(cert["signature"], 64), canonical(b))
            parent = b["child"]
            seen.add(parent)
        if parent != child or chain[-1]["body"]["parent"] != approver:
            raise PermissionError("Approval must be signed by the requesting device")
