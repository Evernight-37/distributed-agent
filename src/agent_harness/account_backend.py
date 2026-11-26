"""Proof-of-possession account API; the service never receives local passwords."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from agent_harness.account_identity import (
    account_id,
    decode,
    verify_envelope,
    canonical,
)
from agent_harness.collaboration_store import account_display_name, digest
from agent_harness.owner_backend import OwnerBackend, PROJECT_AGENT_ACTIONS, PROJECT_AGENT_SETTINGS, execution_project
from agent_harness.protocol import hub_advertisement
from agent_harness.hub_api import HubAccountAPI
from agent_harness.health import HealthCode, safe_message
from agent_harness.common import read_json, write_json


MAX_ACTIVE_CHALLENGES = 4096
MAX_PENDING_DEVICE_REQUESTS = 1000
MAX_PENDING_REQUESTS_PER_KEY = 5
MCP_APPROVAL_KINDS = frozenset({"permission", "attachment", "execution"})
MCP_POLICY_KINDS = MCP_APPROVAL_KINDS | {"memory"}
MCP_PROJECT_APPROVALS = "mcp_project_approvals_v1"


def device_node(public):
    return "device-" + hashlib.sha256(public.encode()).hexdigest()[:24]


# Which client a delegate speaks through. Messages keep the human's identity; the
# label only records the origin, so a reply from Codex and one typed by the person
# are the same principal.
DELEGATE_CLIENTS = {"codex": "codex", "dsh": "dsh"}


def client_label(value):
    return DELEGATE_CLIENTS.get(value) if isinstance(value, str) else None


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

    def _transfer_key(self, node):
        """The public key a device published for signing direct downloads."""
        if not isinstance(node, str) or not node:
            return {}
        value = read_json(self.store.root / "nodes" / (node + ".json"), {}, tolerant=True)
        key = value.get("transfer_public_key") if isinstance(value, dict) else None
        return {"transfer_public_key": key} if isinstance(key, str) and key else {}

    def _with_transfer_keys(self, value):
        """A sender needs the target device's key before it can serve anything."""
        for device in value.get("devices", []):
            device.update(self._transfer_key(device.get("node")))
        for contact in value.get("contacts", []):
            for device in contact.get("devices", []):
                device.update(self._transfer_key(device.get("node")))
        return value

    def _publish_transfer_key(self, user, node, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise ValueError("无效的传输公钥")
        try:
            raw = base64.b64decode(key.encode("ascii"), validate=True)
        except Exception:
            raise ValueError("无效的传输公钥") from None
        if len(raw) != 32:
            raise ValueError("无效的传输公钥")
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM nodes WHERE name=? AND owner_id=?", (node, user)).fetchone():
                raise PermissionError("当前设备尚未接入此协作服务")
        path = self.store.root / "nodes" / (node + ".json")
        current = read_json(path, {}, tolerant=True)
        if not isinstance(current, dict):
            current = {}
        current["transfer_public_key"] = key
        write_json(path, current)
        return {"published": True, "node": node, "transfer_public_key": key}

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
                    "addressed_wake_v1",
                    "task_trace_v1",
                    "project_work_v1",
                    "project_work_cards_v1",
                    "project_lifecycle_v1",
                    "project_memory_review_v1",
                    "project_model_capabilities_v1",
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
                    MCP_PROJECT_APPROVALS,
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
                    "SELECT * FROM account_requests WHERE id=? AND code_hash=?",
                    (data["request_id"], digest(data["code"])),
                ).fetchone()
                if not row:
                    raise PermissionError("关联码无效或已过期（10 分钟有效）：请重新生成")
                if row["claimed"]:
                    raise PermissionError("这个关联码已经被使用过了：请重新生成")
                if row["expires"] <= time.time():
                    raise PermissionError("关联码已过期（10 分钟有效）：请重新生成")
                if not row["account_id"]:
                    return {"state": "pending", "status": "pending"}
                device = db.execute(
                    "SELECT revoked FROM account_devices WHERE public_key=?",
                    (row["public_key"],),
                ).fetchone()
                if not device or device["revoked"]:
                    raise PermissionError("关联已被撤销：请重新生成关联码")
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
                archived = db.execute(
                    "SELECT archived_at FROM projects WHERE id=?", (invitation["project_id"],)
                ).fetchone()
                if not archived or archived["archived_at"] is not None:
                    raise ValueError("项目已归档，请先恢复项目")
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
            delegated_decision = delegate_scopes is not None and body["operation"] == "mcp-approval-decide"
            if delegated_decision:
                db.execute("BEGIN IMMEDIATE")
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
                    "task-cancel": "collaboration.task",
                    "task-status": "collaboration.read",
                    "project-work": "collaboration.task",
                    "memory-note-review": "collaboration.task",
                    "mcp-approvals": "collaboration.read",
                    "mcp-approval-decide": "collaboration.task",
                }.get(body["operation"])
                if not required or required not in delegate_scopes:
                    raise PermissionError("Delegate scope does not allow this operation")
                if delegated_decision:
                    return self._mcp_approval_decide(db, user, public, body["payload"])
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

    def _project_agent_controls(self, db, result, project, user, delegated):
        employees = {
            (row["node"], execution_project(row["id"], project)): row
            for row in db.execute(
                "SELECT id,node,status,settings FROM employees WHERE owner_id IN "
                "(SELECT user_id FROM project_users WHERE project_id=?)",
                (project,),
            )
        }
        legacy_dispatch = {}
        applied = {}
        pending = {}
        for row in db.execute(
            "SELECT id,node,owner_id,state,envelope,result FROM owner_commands "
            "WHERE json_extract(envelope,'$.body.data.project_id')=? ORDER BY created_at,rowid",
            (project,),
        ):
            body = json.loads(row["envelope"])["body"]
            action, data = body["action"], body["data"]
            if row["state"] == "applied" and action in {"dispatch", "withdraw"}:
                legacy_dispatch[data["id"]] = action
            if action not in PROJECT_AGENT_ACTIONS:
                continue
            backend = (
                execution_project(data["id"], project) if action == "project-agent-create" else data["backend_project"]
            )
            key = (row["node"], backend)
            report = json.loads(row["result"] or "{}")
            if row["state"] == "applied":
                applied[key] = (action, report)
            if not delegated and row["owner_id"] == user:
                pending[key] = {
                    "id": row["id"],
                    "action": action,
                    "state": row["state"],
                    "backend_project": backend,
                    "name": data.get("name", ""),
                    "error": safe_message(report.get("error"), "操作失败") if row["state"] == "failed" else None,
                }
        members = []
        for member in result["members"]:
            key = (member["node"], member["backend_project"])
            scoped = applied.get(key)
            employee = employees.get(key) if scoped is None else None
            if scoped and scoped[0] == "project-agent-delete":
                continue
            if employee and (employee["status"] == "deleted" or legacy_dispatch.get(employee["id"]) == "withdraw"):
                continue
            member["employee_id"] = employee["id"] if employee else None
            last_seen = db.execute("SELECT last_seen FROM pull_devices WHERE node=?", (member["node"],)).fetchone()
            member["online"] = bool(last_seen and last_seen["last_seen"] > time.time() - 45)
            member["can_manage_project_agent"] = not delegated and member["owner_id"] == user
            if member["can_manage_project_agent"]:
                bound = db.execute("SELECT stages FROM pull_bindings WHERE agent_id=?", (member["id"],)).fetchone()
                source = (
                    scoped[1].get("settings", {}) if scoped else json.loads(employee["settings"]) if employee else {}
                )
                settings = {key: source[key] for key in PROJECT_AGENT_SETTINGS if key in source}
                actual = {key: member[key] for key in ("model", "effort", "timeout")}
                if scoped:
                    settings = {**actual, **settings}
                else:
                    settings.update(actual)
                settings.setdefault("enabled", bool(member["enabled"]))
                settings.setdefault("stages", json.loads(bound["stages"]) if bound else ["discuss"])
                member["project_agent_settings"] = settings
            members.append(member)
        result["members"] = members
        visible = {member["backend_project"] for member in members}
        result["project_agent_commands"] = [
            item
            for item in pending.values()
            if item["state"] in {"failed", "queued", "claimed"}
            or (item["action"] == "project-agent-create" and item["backend_project"] not in visible)
        ]

    def _attachment_approvals(self, db, user, project=None, *, delegated=False):
        rows = db.execute(
            "SELECT a.*,p.name AS project_name,g.name AS agent_name,t.contract AS task_contract "
            "FROM attachment_approvals a JOIN projects p ON p.id=a.project_id "
            "JOIN agents g ON g.id=a.agent_id JOIN pull_jobs j ON j.id=a.job_id "
            "LEFT JOIN tasks t ON t.job=a.job_id "
            "WHERE a.owner_id=? AND a.state='pending' AND j.cancel_requested=0 "
            "AND j.state NOT IN ('needs_review','completed','failed','cancelled','timed_out','interrupted') "
            "AND (? IS NULL OR a.project_id=?) AND (?=0 OR (g.project_id=a.project_id AND g.owner_id=a.owner_id "
            "AND g.node=a.node AND j.node=a.node AND j.agent_id=a.agent_id "
            "AND EXISTS(SELECT 1 FROM nodes WHERE name=a.node AND owner_id=a.owner_id) "
            "AND EXISTS(SELECT 1 FROM project_users WHERE project_id=a.project_id AND user_id=a.owner_id))) "
            "ORDER BY a.created_at",
            (user, project, project, delegated),
        ).fetchall()
        requests = []
        for row in rows:
            contract = json.loads(row["task_contract"] or "{}")
            recipient = contract.get("reply_to") if contract.get("private_turn") else None
            target = "项目群聊"
            manifest = json.loads(row["manifest"])
            if recipient:
                if delegated and recipient != user:
                    target = "私聊"
                    manifest = [
                        {key: file[key] for key in ("name", "size", "sha256") if key in file} for file in manifest
                    ]
                else:
                    person = db.execute("SELECT username FROM users WHERE id=?", (recipient,)).fetchone()
                    target = "与 " + (person["username"] if person else "成员") + " 的私聊"
            requests.append(
                {
                    key: row[key]
                    for key in (
                        "id",
                        "job_id",
                        "project_id",
                        "project_name",
                        "agent_id",
                        "agent_name",
                        "node",
                        "digest",
                        "created_at",
                    )
                }
                | {"manifest": manifest, "destination": target}
            )
        return requests

    def _attachment_decide(self, db, user, approval_id, approve, *, project=None, expected_digest=None):
        if not isinstance(approval_id, str) or type(approve) is not bool:
            raise ValueError("无效的附件批准决定")
        row = db.execute("SELECT * FROM attachment_approvals WHERE id=? AND owner_id=?", (approval_id, user)).fetchone()
        if not row or project is not None and row["project_id"] != project:
            raise PermissionError("附件请求不存在或不属于当前账户")
        if project is not None and (
            not isinstance(expected_digest, str)
            or not expected_digest.isascii()
            or not hmac.compare_digest(expected_digest, row["digest"])
        ):
            raise PermissionError("附件清单不匹配，请重新查看申请")
        if (
            project is not None
            and not db.execute(
                "SELECT 1 FROM agents g JOIN nodes n ON n.name=g.node JOIN pull_jobs j ON j.agent_id=g.id AND j.node=g.node "
                "WHERE g.id=? AND g.project_id=? AND g.owner_id=? AND n.owner_id=? AND g.node=? AND j.id=? AND j.kind='task'",
                (row["agent_id"], project, user, user, row["node"], row["job_id"]),
            ).fetchone()
        ):
            raise PermissionError("附件请求的执行设备已变更")
        desired = "approved" if approve else "rejected"
        if row["state"] != "pending":
            if row["state"] != desired:
                raise PermissionError("附件请求已经处理或结束")
        else:
            job = db.execute("SELECT state,cancel_requested FROM pull_jobs WHERE id=?", (row["job_id"],)).fetchone()
            if (
                not job
                or job["state"] in {"needs_review", "completed", "failed", "cancelled", "timed_out", "interrupted"}
                or job["cancel_requested"]
            ):
                raise PermissionError("任务已结束或已取消")
            db.execute(
                "UPDATE attachment_approvals SET state=?,updated_at=? WHERE id=?", (desired, time.time(), approval_id)
            )
            self.store.emit_account_event(db, [user], "approval", row["project_id"])
        return {"id": approval_id, "state": desired, "digest": row["digest"]}

    def _mcp_approval_kinds(self, db, user, project):
        return [
            row["kind"]
            for row in db.execute(
                "SELECT kind FROM mcp_approval_policies WHERE project_id=? AND owner_id=? ORDER BY kind",
                (project, user),
            )
        ]

    def _set_mcp_approval_policy(self, user, project, kinds):
        if (
            not isinstance(project, str)
            or not project
            or not isinstance(kinds, list)
            or any(not isinstance(kind, str) or kind not in MCP_POLICY_KINDS for kind in kinds)
        ):
            raise ValueError("无效的项目委托审批设置")
        kinds = sorted(set(kinds))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.require_member(db, project, user)
            if self._mcp_approval_kinds(db, user, project) != kinds:
                db.execute("DELETE FROM mcp_approval_policies WHERE project_id=? AND owner_id=?", (project, user))
                db.executemany(
                    "INSERT INTO mcp_approval_policies(project_id,owner_id,kind) VALUES(?,?,?)",
                    [(project, user, kind) for kind in kinds],
                )
                self.store.log(db, user, "mcp_approval_policy", {"project": project, "kinds": kinds})
                self.store.emit_account_event(db, [user], "approval", project)
        return {"kinds": kinds}

    def _mcp_approvals(self, user, project, delegate_scopes):
        from agent_harness.execution_approvals import execution_requests

        if project is not None and (not isinstance(project, str) or not project):
            raise ValueError("无效的项目编号")
        with self.store.connect() as db:
            if project is not None:
                self.store.require_member(db, project, user)
            permitted = {
                (row["project_id"], row["kind"])
                for row in db.execute("SELECT project_id,kind FROM mcp_approval_policies WHERE owner_id=?", (user,))
            }
            requests = [
                {**row, "kind": "permission"}
                for row in db.execute(
                    "SELECT r.id,r.project_id,r.agent_id,r.node,r.reason,r.state,r.created_at,"
                    "g.name AS agent_name,p.name AS project_name "
                    "FROM permission_requests r JOIN projects p ON p.id=r.project_id JOIN agents g ON g.id=r.agent_id "
                    "JOIN nodes n ON n.name=r.node JOIN project_users m ON m.project_id=r.project_id AND m.user_id=r.owner_id "
                    "WHERE r.owner_id=? AND r.state='pending' AND (? IS NULL OR r.project_id=?) "
                    "AND g.project_id=r.project_id AND g.owner_id=r.owner_id AND g.node=r.node AND n.owner_id=r.owner_id",
                    (user, project, project),
                )
            ]
            requests.extend(
                {**row, "kind": "attachment", "state": "pending"}
                for row in self._attachment_approvals(db, user, project, delegated=True)
            )
            requests.extend(execution_requests(db, user, project))
        can_delegate_decide = "collaboration.task" in (delegate_scopes or ())
        for request in requests:
            request["can_decide"] = (
                can_delegate_decide
                and request.get("can_decide", True)
                and (request["project_id"], request["kind"]) in permitted
            )
        return {"requests": sorted(requests, key=lambda row: (row["created_at"], row["id"]))}

    def _mcp_approval_decide(self, db, user, public, data):
        project, kind, request_id, approve = (data.get(key) for key in ("project", "kind", "id", "approve"))
        if (
            not all(isinstance(value, str) and 1 <= len(value) <= 160 for value in (project, kind, request_id))
            or kind not in MCP_APPROVAL_KINDS
            or type(approve) is not bool
        ):
            raise ValueError("无效的委托审批决定")
        self.store.require_member(db, project, user)
        if kind not in self._mcp_approval_kinds(db, user, project):
            raise PermissionError("当前账户没有为这个项目开启该类委托审批")
        if kind == "permission":
            result = self.store.permission_decide(user, project, request_id, approve, db=db)
        elif kind == "attachment":
            result = self._attachment_decide(
                db, user, request_id, approve, project=project, expected_digest=data.get("digest")
            )
        else:
            from agent_harness.execution_approvals import decide_execution

            result = decide_execution(db, user, project, request_id, approve, data.get("digest"))
            self.store.emit_account_event(db, [user], "approval", project)
        self.store.log(
            db,
            user,
            "mcp_approval_decide",
            {
                "delegate": digest(public),
                "project": project,
                "kind": kind,
                "request": request_id,
                "state": result["state"],
                "client": client_label(data.get("client")) or "codex",
            },
        )
        return {"project_id": project, "kind": kind, **result}

    def _operation(self, user, public, op, p, delegate_scopes=None):
        if op == "mcp-approval-policy":
            if delegate_scopes is not None:
                raise PermissionError("委托身份不能修改审批授权设置")
            return self._set_mcp_approval_policy(user, p.get("project"), p.get("kinds"))
        if op == "mcp-approvals":
            return self._mcp_approvals(user, p.get("project"), delegate_scopes)
        if op == "attachment-approvals":
            with self.store.connect() as db:
                return {"requests": self._attachment_approvals(db, user)}
        if op == "attachment-decide":
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                return self._attachment_decide(db, user, p.get("id"), p.get("approve"))
        if op == "health-status":
            return self.store.health_status()
        if op == "events-wait":
            return self.store.events_wait(
                user,
                p.get("cursor", 0),
                p.get("wait_seconds", 0),
                attention=p.get("attention", False) if delegate_scopes is None else False,
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
                    config = read_json(self.store.root / "nodes" / (node + ".json"), {}, tolerant=True)
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
                            "encrypt_public_key": config.get("encrypt_public_key"),
                            "transfer_public_key": config.get("transfer_public_key"),
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
                    if employee["node"] not in device_map:
                        # The machine that hosted this employee was removed or was
                        # re-associated with a new identity; its queued commands
                        # can never be delivered.
                        employee["status"] = "orphaned"
                        employee["status_code"] = HealthCode.NOT_CONFIGURED
                        continue
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
            result = self.store.project_view(user, project, delegate=public if delegate_scopes is not None else None)
            from agent_harness.project_memory import ProjectMemory
            from agent_harness.project_work import ProjectWork

            result["project_memory"] = ProjectMemory(self.store).snapshot(user, project)
            with self.store.connect() as db:
                project_work = ProjectWork(self.store)
                result["work"] = project_work.view(db, user, project)
                cards = project_work.project_cards(db, user, project, result["messages"])
                for message in result["messages"]:
                    if message["id"] in cards:
                        message["project_work_card"] = cards[message["id"]]
                if p.get("visit") is True:
                    db.execute(
                        "INSERT INTO account_project_visits VALUES(?,?,?) ON CONFLICT(account_id,project_id) DO UPDATE SET visited=excluded.visited",
                        (user, project, time.time()),
                    )
                self._project_agent_controls(db, result, project, user, delegate_scopes is not None)
                if delegate_scopes is None:
                    result["mcp_approval_kinds"] = self._mcp_approval_kinds(db, user, project)
                result["project_memory"]["can_review"] = result["project"]["archived_at"] is None and (
                    delegate_scopes is None
                    or "collaboration.task" in delegate_scopes
                    and "memory" in self._mcp_approval_kinds(db, user, project)
                )
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
        if op == "project-lifecycle":
            from agent_harness.project_lifecycle import ProjectLifecycle

            return ProjectLifecycle(self.store).command(user, p["project"], p["action"], p.get("confirmation"))
        if op in {"join", "join-project"}:
            return self.store.join(user, p["invite"])
        if op == "invite":
            return self.store.invite(user, p["project"])
        if op == "message":
            words = str(p.get("body") or "").strip().split(maxsplit=1)
            if delegate_scopes is not None and words and words[0] in {"/allow", "/deny"}:
                raise PermissionError("委托身份不能批准或撤销 Agent 权限")
            return self.store.message(
                user,
                p["project"],
                p["body"],
                p.get("recipient_id"),
                p.get("mention_agent_id"),
                p.get("conversation_id"),
                p.get("client_message_id"),
                (client_label(p.get("client")) or "codex") if delegate_scopes is not None else None,
                p.get("attachment_ids"),
            )
        if op in {"attachment-upload", "attachment-get"}:
            from agent_harness.attachments import Attachments

            files = Attachments(self.store)
            if op == "attachment-upload":
                return files.upload(user, p["project"], p["name"], p["base64"])
            return files.download(user, p["project"], p["id"])
        if op == "transfer-peers":
            if delegate_scopes is not None:
                raise PermissionError("Codex 委托身份不能操作设备文件传输")
            from agent_harness.direct_signaling import peers

            return self._with_transfer_keys(peers(self.store, user))
        if op in {
            "transfer-direct-offer",
            "transfer-direct-respond",
            "transfer-direct-list",
            "transfer-direct-progress",
            "transfer-key-publish",
        }:
            from agent_harness.direct_signaling import Transfers

            direct = Transfers(self.store)
            if op == "transfer-direct-list":
                return {"transfers": direct.view(user)}
            if op == "transfer-key-publish":
                return self._publish_transfer_key(user, device_node(public), p.get("transfer_public_key"))
            if op == "transfer-direct-progress":
                return direct.progress(
                    user,
                    p["id"],
                    p["transferred"],
                    p.get("state"),
                    p.get("error"),
                )
            if op == "transfer-direct-offer":
                return direct.offer(
                    user,
                    device_node(public),
                    p.get("to_node"),
                    p["name"],
                    p["size"],
                    p["address"],
                    recipient=p.get("recipient"),
                    transfer_id=p.get("id"),
                )
            # Accepting on behalf of a device has to say which device, and which
            # key will sign its download; the sender can verify nothing otherwise.
            return direct.respond(
                user,
                p["id"],
                bool(p["accept"]),
                node=p.get("node"),
                claim_key=p.get("claim_key"),
            )
        if op == "conversation-read":
            return self.store.mark_conversation_read(user, p["project"], p["conversation_id"])
        if op == "project-work":
            from agent_harness.project_work import ProjectWork

            return ProjectWork(self.store).command(user, p, public if delegate_scopes is not None else None)
        if op == "task":
            if delegate_scopes is not None:
                p = {**p, "requested_via": client_label(p.get("client")) or "codex"}
            return self.store.task(user, p, delegate=public if delegate_scopes is not None else None)
        if op == "task-cancel":
            return self.store.cancel_task(
                user, p["project"], p["task_id"], public if delegate_scopes is not None else None
            )
        if op == "agent-permission":
            if delegate_scopes is not None:
                raise PermissionError("委托身份不能批准或撤销 Agent 权限")
            return self.store.agent_permission(user, p["project"], p["agent_id"], p["allow"])
        if op == "permission-decide":
            if delegate_scopes is not None:
                raise PermissionError("委托身份不能批准 Agent 的提权请求")
            return self.store.permission_decide(user, p["project"], p["request_id"], p["approve"])
        if op == "task-status":
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM tasks WHERE id=?", (p.get("task"),)).fetchone()
                if not row or "project" in p and row["project_id"] != p["project"]:
                    raise ValueError("未知任务")
                self.store.require_member(db, row["project_id"], user)
                if not self.store.task_visible_to_human(db, row, user):
                    raise PermissionError("无权查看此任务")
                result = dict(row)
                result["trace"] = self.store.task_trace_for_human(db, row, user)
                from agent_harness.run_history import task_run_details

                result["run_details"] = task_run_details(db, row["job"])
                recovery = self.store.local_task_recovery(
                    db, row, user, public if delegate_scopes is not None else None
                )
                if recovery:
                    result["local_recovery"] = recovery
                return {"ok": True, "result": result}
        if op == "memory-note-add":
            from agent_harness.project_memory import ProjectMemory

            return ProjectMemory(self.store).add_note(user, p["project"], p["body"], kind=p.get("kind"))
        if op == "memory-note-review":
            from agent_harness.project_memory import ProjectMemory

            return ProjectMemory(self.store).review_notes(
                user,
                p["project"],
                p["ids"],
                p["action"],
                body=p.get("body"),
                kind=p.get("kind"),
                delegate=public if delegate_scopes is not None else None,
            )
        if op == "memory-note-delete":
            from agent_harness.project_memory import ProjectMemory

            return ProjectMemory(self.store).delete_note(user, p["project"], p.get("id", p.get("note_id")))
        if op == "set-admin":
            if p.get("agent"):
                return self.store.set_admin(user, p["project"], p["agent"]) or {"updated": True}
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                project = self.store.require_member(db, p["project"], user, active=True)
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
                    "SELECT id,public_key,name,account_id,expires,claimed FROM account_requests WHERE code_hash=?",
                    (digest(p["code"]),),
                ).fetchone()
                if not row:
                    raise PermissionError("关联码无效或已过期（10 分钟有效）：请在那台机器上重新生成")
                if row["account_id"]:
                    raise PermissionError("这个关联码已经被使用过了：请在那台机器上重新生成")
                if row["expires"] <= time.time():
                    raise PermissionError("关联码已过期（10 分钟有效）：请在那台机器上重新生成")
                if row["claimed"]:
                    raise PermissionError("这台设备已经认领过该关联码：请在那台机器上重新生成")
                return dict(row, request_id=row["id"])
        if op == "approve-device":
            code = p.get("code")
            rid = p.get("request_id")
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM account_requests WHERE " + ("id=?" if rid else "code_hash=?") + "",
                    (rid or digest(code or ""),),
                ).fetchone()
                if not row:
                    raise PermissionError("关联码无效或已过期（10 分钟有效）：请在那台机器上重新生成")
                if row["account_id"]:
                    raise PermissionError("这个关联码已经被使用过了：请在那台机器上重新生成")
                if row["expires"] <= time.time():
                    raise PermissionError("关联码已过期（10 分钟有效）：请在那台机器上重新生成")
                if row["claimed"]:
                    raise PermissionError("这台设备已经认领过该关联码：请在那台机器上重新生成")
                chain = p["chain"]
                # Child proof is not available to approving device. Verify certificates via
                # their parent signatures separately, using certificate helper below.
                self._verify_chain(chain, user, row["public_key"], public)
                existing = db.execute(
                    "SELECT revoked FROM account_devices WHERE public_key=?",
                    (row["public_key"],),
                ).fetchone()
                if existing and not existing["revoked"]:
                    raise ValueError("这台设备已经关联过了，无需再次关联")
                if existing:
                    # Revoked rows are kept for the audit trail; approving the same
                    # machine again must reactivate it instead of failing the
                    # primary key or adding a second row for one device.
                    db.execute(
                        "UPDATE account_devices SET account_id=?,name=?,chain=?,revoked=0 WHERE public_key=?",
                        (user, row["name"], json.dumps(chain), row["public_key"]),
                    )
                else:
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
        if op == "rename-device":
            # Display name only: the device is identified by its public key, so a
            # rename never changes what the machine is or what it may do.
            target = p.get("id") or p.get("public_key")
            name = self._name(p.get("name"))
            with self.store.connect() as db:
                rows = db.execute(
                    "SELECT public_key FROM account_devices WHERE account_id=? AND revoked=0",
                    (user,),
                ).fetchall()
                match = next(
                    (
                        row["public_key"]
                        for row in rows
                        if target in {row["public_key"], device_node(row["public_key"])}
                    ),
                    None,
                )
                if match is None:
                    raise PermissionError("Unknown device")
                db.execute(
                    "UPDATE account_devices SET name=? WHERE public_key=?",
                    (name, match),
                )
            return {"renamed": True, "name": name}
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
        return account_display_name(user, name)

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
