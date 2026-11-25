"""Owner-signed device settings queue; ordinary project tasks cannot change devices."""

import json
import hashlib
import time
from agent_harness.account_identity import verify_owner_command


def device_node(public):
    """Node address derived from a device public key."""
    return "device-" + hashlib.sha256(public.encode()).hexdigest()[:24]


def execution_project(identity, project):
    return "e-" + hashlib.sha256((identity + "\0" + project).encode()).hexdigest()[:56]


PROJECT_AGENT_ACTIONS = {"project-agent-create", "project-agent-configure", "project-agent-delete"}
PROJECT_AGENT_SETTINGS = {
    "model",
    "effort",
    "timeout",
    "stages",
    "enabled",
    "strict_worktree",
    "attachment_policy",
    "network",
}
ACTIONS = {
    "create",
    "configure",
    "dispatch",
    "withdraw",
    "browse",
    "delete",
    "workspace-default",
    "transfer-receive-directory",
} | PROJECT_AGENT_ACTIONS
TERMINAL = {"applied", "failed"}


def project_agent_target(action, data):
    """Read the same clear identity and settings shape on Hub and device."""
    create = action == "project-agent-create"
    fields = {"project_id", "id"} if create else {"project_id", "agent_id", "backend_project"}
    if action != "project-agent-delete":
        fields |= {"name", "settings"}
    if set(data) != fields:
        raise ValueError("无效的项目 Agent 操作参数")
    project = data["project_id"]
    identity = data["id" if create else "agent_id"]
    for value in (project, identity, data.get("backend_project", project)):
        if not isinstance(value, str) or not 1 <= len(value) <= 120 or "\0" in value:
            raise ValueError("无效的项目 Agent 标识")
    if action != "project-agent-delete":
        name, settings = data["name"], data["settings"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError("Agent 名称必须为 1 到 120 个字符")
        if not isinstance(settings, dict) or set(settings) - PROJECT_AGENT_SETTINGS:
            raise ValueError("无效的项目 Agent 配置")
    return project, identity, execution_project(identity, project) if create else data["backend_project"]


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class OwnerBackend:
    def __init__(self, store):
        self.store = store

    def snapshot(self, user):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM employees WHERE owner_id=? AND status<>'deleted' ORDER BY name,id",
                (user,),
            ).fetchall()
            return {"employees": [dict(r, settings=json.loads(r["settings"])) for r in rows]}

    def command(self, user, command_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM owner_commands WHERE id=? AND owner_id=?",
                (command_id, user),
            ).fetchone()
            if not row:
                raise PermissionError("Unknown owner command")
            return {
                "id": row["id"],
                "state": row["state"],
                "result": json.loads(row["result"]) if row["result"] else None,
            }

    def enqueue(self, user, envelope):
        verify_owner_command(envelope, expected_account=user)
        body = envelope["body"]
        if body.get("domain") != "harness-owner-command-v1" or body.get("account_id") != user:
            raise PermissionError("Invalid command identity")
        action, target, cid, data = (body.get(k) for k in ("action", "target", "id", "data"))
        if action not in ACTIONS or not isinstance(data, dict):
            raise ValueError("Invalid owner action")
        if not isinstance(cid, str) or not 1 <= len(cid) <= 120:
            raise ValueError("Invalid command id")
        encoded = canonical(envelope)
        if len(encoded) > 100000:
            raise ValueError("Owner command too large")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            node = db.execute("SELECT owner_id FROM nodes WHERE name=?", (target,)).fetchone()
            if not node or node["owner_id"] != user:
                raise PermissionError("Device belongs to another account")
            previous = db.execute("SELECT * FROM owner_commands WHERE id=?", (cid,)).fetchone()
            if previous:
                if previous["owner_id"] != user or canonical(json.loads(previous["envelope"])["body"]) != canonical(
                    body
                ):
                    raise ValueError("Command id already used")
                return {"id": cid, "state": previous["state"], "duplicate": True}
            if action == "workspace-default":
                project_id = data.get("project_id")
                if not isinstance(project_id, str) or not 1 <= len(project_id) <= 120:
                    raise ValueError("Invalid shared project")
                self.store.require_member(db, project_id, user)
            elif action in PROJECT_AGENT_ACTIONS:
                if body.get("sealed") is not None:
                    raise ValueError("项目 Agent 操作必须使用可核对的项目标识")
                project, identity, backend = project_agent_target(action, data)
                self.store.require_member(db, project, user)
                agent = db.execute(
                    "SELECT * FROM agents WHERE node=? AND backend_project=?", (target, backend)
                ).fetchone()
                if action == "project-agent-create":
                    if (
                        agent
                        or db.execute(
                            "SELECT 1 FROM owner_commands WHERE node=? AND state IN ('queued','claimed','applied') "
                            "AND json_extract(envelope,'$.body.action')='project-agent-create' "
                            "AND json_extract(envelope,'$.body.data.project_id')=? "
                            "AND json_extract(envelope,'$.body.data.id')=?",
                            (target, project, identity),
                        ).fetchone()
                    ):
                        raise ValueError("这个项目 Agent 已经创建或正在创建")
                else:
                    if not agent or (agent["id"], agent["project_id"], agent["owner_id"]) != (identity, project, user):
                        raise PermissionError("项目 Agent 不属于这个项目或设备")
                    if self._agent_removed(db, target, identity):
                        raise ValueError("项目 Agent 已经移除")
                    if self._pending_agent_change(db, target, identity):
                        raise ValueError("该项目 Agent 还有待确认的配置操作")
                    self._require_agent_idle(db, identity)
                    if action == "project-agent-delete" and not self._live_node(db, user, target):
                        self._remove_agent(db, agent, user)
                        result = {"project_id": project, "backend_project": backend, "enabled": False}
                        db.execute(
                            "INSERT INTO owner_commands VALUES(?,?,?,?,?,?,?)",
                            (cid, user, target, encoded, "applied", canonical(result), time.time()),
                        )
                        self.store.emit_project_event(db, project, "agent")
                        return {"id": cid, "state": "applied", "applied": True}
                    db.execute("UPDATE agents SET configuring=1 WHERE id=?", (identity,))
            elif action == "transfer-receive-directory":
                path = data.get("path")
                if not isinstance(path, str) or not 1 <= len(path) <= 4096 or "\0" in path:
                    raise ValueError("Invalid receive directory")
            elif action != "browse":
                employee = data.get("id")
                if not isinstance(employee, str) or not 1 <= len(employee) <= 120:
                    raise ValueError("Invalid employee id")
                old = db.execute("SELECT * FROM employees WHERE id=?", (employee,)).fetchone()
                if action == "create":
                    if old:
                        raise ValueError("Employee already exists")
                    name = data.get("name")
                    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                        raise ValueError("Invalid employee name")
                    db.execute(
                        "INSERT INTO employees(id,name,owner_id,node,settings,status) VALUES(?,?,?,?,?,?)",
                        (employee, name.strip(), user, target, "{}", "pending"),
                    )
                elif not old or old["owner_id"] != user or old["node"] != target:
                    raise PermissionError("Employee ownership mismatch")
                elif old["status"] == "deleted" and action != "delete":
                    raise ValueError("员工已删除")
                if action == "delete":
                    for agent in self._employee_agents(db, employee, user, target):
                        self._require_agent_idle(db, agent["id"])
                if action in {"dispatch", "withdraw"}:
                    self.store.require_member(db, data.get("project_id"), user)
                if action == "delete" and not self._live_node(db, user, target):
                    # That machine is no longer an active device, so no command can
                    # ever be delivered; waiting for its acknowledgement is what
                    # kept such an employee undeletable.
                    self._apply_delete(db, employee, user, target)
                    db.execute(
                        "DELETE FROM owner_commands WHERE owner_id=? AND node=? AND state IN ('queued','claimed')",
                        (user, target),
                    )
                    self.store.emit_account_event(db, [user], "owner-command")
                    return {"id": None, "state": "applied", "applied": True}
            db.execute(
                "INSERT INTO owner_commands VALUES(?,?,?,?,?,?,?)",
                (cid, user, target, encoded, "queued", None, time.time()),
            )
            self.store.emit_account_event(db, [user], "owner-command")
            return {"id": cid, "state": "queued"}

    def _live_node(self, db, owner, node):
        """Whether a command to this node can still be delivered.

        A re-associated machine keeps nothing of its old identity: the old node is
        still a row in ``nodes`` but its device entry is revoked, so the queue has
        nowhere to go.
        """
        if not db.execute(
            "SELECT 1 FROM nodes WHERE name=? AND owner_id=?",
            (node, owner),
        ).fetchone():
            return False
        for row in db.execute(
            "SELECT public_key, revoked FROM account_devices WHERE account_id=?",
            (owner,),
        ):
            if device_node(row["public_key"]) == node:
                return not row["revoked"]
        return True

    def _apply_delete(self, db, employee, owner, node):
        for agent in self._employee_agents(db, employee, owner, node):
            self._remove_agent(db, agent, owner)
        db.execute(
            "UPDATE employees SET status='deleted',revision=revision+1 WHERE id=?",
            (employee,),
        )

    def _employee_agents(self, db, employee, owner, node):
        return [
            a
            for a in db.execute("SELECT * FROM agents WHERE owner_id=? AND node=?", (owner, node))
            if a["backend_project"] == execution_project(employee, a["project_id"])
            and not db.execute(
                "SELECT 1 FROM owner_commands WHERE node=? AND state IN ('queued','claimed','applied') "
                "AND json_extract(envelope,'$.body.action') IN ('project-agent-configure','project-agent-delete') "
                "AND json_extract(envelope,'$.body.data.agent_id')=?",
                (node, a["id"]),
            ).fetchone()
        ]

    @staticmethod
    def _agent_removed(db, node, agent):
        return db.execute(
            "SELECT 1 FROM owner_commands WHERE node=? AND state='applied' "
            "AND json_extract(envelope,'$.body.action')='project-agent-delete' "
            "AND json_extract(envelope,'$.body.data.agent_id')=?",
            (node, agent),
        ).fetchone()

    @staticmethod
    def _pending_agent_change(db, node, agent):
        return db.execute(
            "SELECT 1 FROM owner_commands WHERE node=? AND state IN ('queued','claimed') "
            "AND json_extract(envelope,'$.body.action') IN ('project-agent-configure','project-agent-delete') "
            "AND json_extract(envelope,'$.body.data.agent_id')=?",
            (node, agent),
        ).fetchone()

    @staticmethod
    def _require_agent_idle(db, agent):
        if db.execute(
            "SELECT 1 FROM tasks WHERE agent_id=? AND state IN ('queued','starting','running','unknown')", (agent,)
        ).fetchone():
            raise ValueError("Agent 有未完成任务，请等待任务结束后再操作")
        if db.execute(
            "SELECT 1 FROM executions WHERE agent_id=? AND state IN ('approved','starting','running','unknown')",
            (agent,),
        ).fetchone():
            raise ValueError("Agent 有未完成执行，请等待执行结束后再操作")

    def _remove_agent(self, db, agent, owner):
        for scope in self.store.grants(agent["id"]):
            self.store.set_grant(agent["id"], scope, False, actor=owner, db=db)
        db.execute("UPDATE agents SET enabled=0,configuring=0 WHERE id=?", (agent["id"],))
        db.execute("UPDATE projects SET admin_agent_id=NULL WHERE admin_agent_id=?", (agent["id"],))

    def poll(self, db, device, claim=True):
        # Revalidate ownership on every poll; no trust in a caller-supplied node alone.
        self._device(db, device)
        rows = db.execute(
            "SELECT * FROM owner_commands WHERE node=? AND owner_id=? AND state IN ('queued','claimed') ORDER BY created_at,id LIMIT 20",
            (device["node"], device["owner_id"]),
        ).fetchall()
        if claim:
            for row in rows:
                db.execute(
                    "UPDATE owner_commands SET state='claimed' WHERE id=? AND state='queued'",
                    (row["id"],),
                )
        return [json.loads(row["envelope"]) for row in rows]

    def _device(self, db, device):
        row = db.execute("SELECT owner_id FROM nodes WHERE name=?", (device.get("node"),)).fetchone()
        if not row or row["owner_id"] != device.get("owner_id"):
            raise PermissionError("Invalid owner device")

    def report(self, db, device, command_id, state, result):
        self._device(db, device)
        row = db.execute("SELECT * FROM owner_commands WHERE id=?", (command_id,)).fetchone()
        if not row or row["node"] != device["node"] or row["owner_id"] != device["owner_id"]:
            raise PermissionError("Invalid owner command lease")
        if state not in TERMINAL or not isinstance(result, dict):
            raise ValueError("Invalid command result")
        encoded = canonical(result)
        if len(encoded) > 100000:
            raise ValueError("Command result too large")
        if row["state"] in TERMINAL:
            if row["state"] != state or row["result"] != encoded:
                raise ValueError("Terminal result cannot change")
            return {"ok": True, "duplicate": True}
        if row["state"] != "claimed":
            raise ValueError("Command has not been claimed")
        body = json.loads(row["envelope"])["body"]
        data = body["data"]
        action = body["action"]
        if action in PROJECT_AGENT_ACTIONS:
            project, identity, backend = project_agent_target(action, data)
            if state == "applied":
                if result.get("project_id") != project or result.get("backend_project") != backend:
                    raise ValueError("设备报告的项目 Agent 身份不一致")
                if action != "project-agent-delete":
                    settings = result.get("settings")
                    if not isinstance(settings, dict) or set(settings) != PROJECT_AGENT_SETTINGS:
                        raise ValueError("设备报告的项目 Agent 配置无效")
                    if result.get("name") != data["name"].strip() or type(result.get("enabled")) is not bool:
                        raise ValueError("设备报告的项目 Agent 名称或状态无效")
                if action == "project-agent-create":
                    db.execute(
                        "INSERT INTO project_workspaces(project_id,owner_id,node,status,write_policy,updated_at) "
                        "VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,node) DO UPDATE SET "
                        "owner_id=excluded.owner_id,status=excluded.status,updated_at=excluded.updated_at",
                        (project, row["owner_id"], row["node"], "ready", "single_writer", time.time()),
                    )
                elif action == "project-agent-delete":
                    agent = db.execute("SELECT * FROM agents WHERE id=?", (identity,)).fetchone()
                    if agent:
                        self._remove_agent(db, agent, row["owner_id"])
                else:
                    model, stages, effort, timeout = (settings[k] for k in ("model", "stages", "effort", "timeout"))
                    if (
                        not isinstance(model, str)
                        or not 1 <= len(model) <= 120
                        or effort not in ("low", "medium", "high", "xhigh", "max", "ultra")
                        or type(timeout) is not int
                        or not 30 <= timeout <= 86400
                        or not isinstance(stages, list)
                        or not stages
                        or any(stage not in ("discuss", "review", "implement") for stage in stages)
                        or settings.get("enabled") is not result["enabled"]
                    ):
                        raise ValueError("设备报告的项目 Agent 配置无效")
                    db.execute(
                        "UPDATE agents SET name=?,model=?,effort=?,timeout=?,enabled=? WHERE id=?",
                        (result["name"], model, effort, timeout, int(result["enabled"]), identity),
                    )
                    db.execute("UPDATE pull_bindings SET stages=? WHERE agent_id=?", (json.dumps(stages), identity))
            if action != "project-agent-create":
                db.execute("UPDATE agents SET configuring=0 WHERE id=?", (identity,))
            self.store.emit_project_event(db, project, "agent")
        elif action == "workspace-default":
            if state == "applied":
                db.execute(
                    "INSERT INTO project_workspaces(project_id,owner_id,node,status,write_policy,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,node) DO UPDATE SET "
                    "owner_id=excluded.owner_id,status=excluded.status,updated_at=excluded.updated_at",
                    (data["project_id"], row["owner_id"], row["node"], "ready", "single_writer", time.time()),
                )
        elif action not in {"browse", "transfer-receive-directory"}:
            if state == "applied" and action == "dispatch":
                db.execute(
                    "INSERT INTO project_workspaces(project_id,owner_id,node,status,write_policy,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,node) DO UPDATE SET "
                    "owner_id=excluded.owner_id,status=excluded.status,updated_at=excluded.updated_at",
                    (data["project_id"], row["owner_id"], row["node"], "ready", "single_writer", time.time()),
                )
            elif state == "applied" and action == "delete":
                self._apply_delete(db, data["id"], row["owner_id"], row["node"])
            elif state == "applied" and action in {"create", "configure"}:
                settings = result.get("settings", {})
                if not isinstance(settings, dict):
                    raise ValueError("Invalid applied settings")
                db.execute(
                    "UPDATE employees SET settings=?,name=?,status=?,revision=revision+1 WHERE id=? AND status<>'deleted'",
                    (
                        canonical(settings),
                        result.get("name", data.get("name", ""))
                        or db.execute("SELECT name FROM employees WHERE id=?", (data["id"],)).fetchone()["name"],
                        "idle",
                        data["id"],
                    ),
                )
            elif state == "failed" and action == "create":
                db.execute(
                    "UPDATE employees SET status='failed' WHERE id=? AND status<>'deleted'",
                    (data["id"],),
                )
        if action == "transfer-receive-directory":
            # The trusted relay must see the signed path while delivering the
            # command, but it has no reason to retain that filesystem detail
            # after the target machine reports a terminal result.
            envelope = json.loads(row["envelope"])
            envelope["body"]["data"] = {"path": "[redacted]"}
            db.execute(
                "UPDATE owner_commands SET state=?,result=?,envelope=? WHERE id=?",
                (state, encoded, canonical(envelope), command_id),
            )
        else:
            db.execute(
                "UPDATE owner_commands SET state=?,result=? WHERE id=?",
                (state, encoded, command_id),
            )
        self.store.emit_account_event(db, [row["owner_id"]], "owner-command")
        return {"ok": True}
