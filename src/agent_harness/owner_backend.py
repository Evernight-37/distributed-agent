"""Owner-signed device settings queue; ordinary project tasks cannot change devices."""

import json
import hashlib
import time
from agent_harness.account_identity import verify_owner_command

ACTIONS = {
    "create",
    "configure",
    "dispatch",
    "withdraw",
    "browse",
    "delete",
    "workspace-default",
    "transfer-receive-directory",
}
TERMINAL = {"applied", "failed"}


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
                        if db.execute(
                            "SELECT 1 FROM tasks WHERE agent_id=? AND state IN ('queued','starting','running','unknown')",
                            (agent["id"],),
                        ).fetchone():
                            raise ValueError("员工有未完成任务，请等待任务结束后再删除")
                        if db.execute(
                            "SELECT 1 FROM executions WHERE agent_id=? AND state IN ('approved','starting','running','unknown')",
                            (agent["id"],),
                        ).fetchone():
                            raise ValueError("员工有未完成执行，请等待执行结束后再删除")
                if action in {"dispatch", "withdraw"}:
                    self.store.require_member(db, data.get("project_id"), user)
            db.execute(
                "INSERT INTO owner_commands VALUES(?,?,?,?,?,?,?)",
                (cid, user, target, encoded, "queued", None, time.time()),
            )
            return {"id": cid, "state": "queued"}

    def _employee_agents(self, db, employee, owner, node):
        return [
            a
            for a in db.execute("SELECT * FROM agents WHERE owner_id=? AND node=?", (owner, node))
            if a["backend_project"]
            == "e-" + hashlib.sha256((employee + "\0" + a["project_id"]).encode()).hexdigest()[:56]
        ]

    def poll(self, db, device):
        # Revalidate ownership on every poll; no trust in a caller-supplied node alone.
        self._device(db, device)
        rows = db.execute(
            "SELECT * FROM owner_commands WHERE node=? AND owner_id=? AND state IN ('queued','claimed') ORDER BY created_at,id LIMIT 20",
            (device["node"], device["owner_id"]),
        ).fetchall()
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
        if action == "workspace-default":
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
                for agent in self._employee_agents(db, data["id"], row["owner_id"], row["node"]):
                    db.execute(
                        "UPDATE agents SET enabled=0,configuring=0 WHERE id=?",
                        (agent["id"],),
                    )
                    db.execute(
                        "UPDATE projects SET admin_agent_id=NULL WHERE admin_agent_id=?",
                        (agent["id"],),
                    )
                db.execute(
                    "UPDATE employees SET status='deleted',revision=revision+1 WHERE id=?",
                    (data["id"],),
                )
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
        return {"ok": True}
