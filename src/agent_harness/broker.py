"""Durable pull-only coordination. Device credentials confer no host execution access."""

import hmac
import json
import re
import secrets
import time
from agent_harness.collaboration_store import uid, digest
from agent_harness.protocol import hub_advertisement
from agent_harness.common import read_json, write_json
from agent_harness.health import report as health_report

TERMINAL = {
    "needs_review",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
}


class PullBackend:
    durable_ids = True

    def __init__(self, store, node=None):
        self.store, self.node = store, node

    def for_node(self, node):
        return PullBackend(self.store, node)

    def create_pair(self, user, node):
        if not isinstance(node, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", node):
            raise ValueError("机器名称使用字母、数字、下划线或连字符")
        code = secrets.token_urlsafe(24)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user,)).fetchone():
                raise PermissionError("未知用户")
            existing = db.execute("SELECT owner_id FROM nodes WHERE name=?", (node,)).fetchone()
            if existing and existing["owner_id"] != user:
                raise PermissionError("机器属于其他人")
            db.execute("INSERT OR IGNORE INTO nodes(name,owner_id) VALUES(?,?)", (node, user))
            db.execute("DELETE FROM pull_pairs WHERE node=? OR expires<?", (node, time.time()))
            db.execute(
                "INSERT INTO pull_pairs VALUES(?,?,?,?)",
                (digest(code), node, user, time.time() + 600),
            )
            self.store.log(db, user, "create_device_pair", {"node": node})
        return {"pair_code": code, "node": node, "expires_in": 600}

    def _device(self, db, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise PermissionError("机器认证失败")
        device = db.execute(
            "SELECT d.node,n.owner_id FROM pull_devices d JOIN nodes n ON n.name=d.node WHERE d.token_hash=?",
            (digest(token),),
        ).fetchone()
        if not device:
            raise PermissionError("机器认证失败")
        return dict(device)

    def _projects(self, db, device):
        return [
            dict(r)
            for r in db.execute(
                "SELECT p.id,p.name FROM projects p JOIN project_users m ON m.project_id=p.id WHERE m.user_id=?",
                (device["owner_id"],),
            )
        ]

    def _poll_snapshot(self, db, device):
        rows = db.execute(
            "SELECT * FROM pull_jobs WHERE node=? AND state NOT IN "
            "('needs_review','completed','failed','cancelled','timed_out','interrupted') "
            "ORDER BY created_at LIMIT 20",
            (device["node"],),
        ).fetchall()
        jobs = []
        for row in rows:
            if row["state"] == "queued":
                db.execute(
                    "UPDATE pull_jobs SET state='claimed' WHERE id=? AND state='queued'",
                    (row["id"],),
                )
            jobs.append(
                {
                    "id": row["id"],
                    "job_id": row["id"],
                    "lease_id": row["lease_id"],
                    "kind": row["kind"],
                    "agent_id": row["agent_id"],
                    **self._job_binding(db, row),
                    "request": json.loads(row["request"]),
                }
            )
        from agent_harness.owner_backend import OwnerBackend

        controls = OwnerBackend.__new__(OwnerBackend)
        controls.store = self.store
        commands = (
            controls.poll(db, device)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='owner_commands'").fetchone()
            else []
        )
        return {
            "protocol": hub_advertisement(),
            "features": ["device_file_transfers_v1"],
            "owner_commands": commands,
            "node": device["node"],
            "projects": self._projects(db, device),
            "jobs": jobs,
            "cancellations": [row["id"] for row in rows if row["cancel_requested"]],
        }

    def _poll(self, data, bearer):
        wait_seconds = data.get("wait_seconds", 0)
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 10:
            raise ValueError("wait_seconds must be between 0 and 10")
        deadline = time.monotonic() + wait_seconds
        first = True
        while True:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                device = self._device(db, bearer)
                if first:
                    db.execute(
                        "UPDATE pull_devices SET last_seen=? WHERE node=?",
                        (time.time(), device["node"]),
                    )
                    if "bindings" in data:
                        self._advertise(db, device, data["bindings"])
                    self._record_health(device, data.get("health"))
                result = self._poll_snapshot(db, device)
            if result["jobs"] or result["owner_commands"] or result["cancellations"] or time.monotonic() >= deadline:
                return result
            first = False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result
            time.sleep(min(0.25, remaining))

    def _record_health(self, device, value):
        """Persist only the bounded health-v1 allowlist, never raw diagnostics."""
        if not isinstance(value, dict):
            return
        components = value.get("components")
        if not isinstance(components, list) or len(components) > 16:
            return
        try:
            safe = health_report(components, value.get("generated_at"))
        except (TypeError, ValueError):
            return
        path = self.store.root / "nodes" / (device["node"] + ".json")
        current = read_json(path, {})
        if not isinstance(current, dict):
            current = {}
        current["health"] = safe
        write_json(path, current)

    def device_api(self, path, data, bearer=None):
        if not isinstance(data, dict):
            raise ValueError("请求必须为对象")
        if path == "/api/device/enroll":
            token = data.get("device_token")
            if not isinstance(token, str) or not 32 <= len(token) <= 256:
                raise ValueError("无效的机器令牌")
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                pair = db.execute(
                    "SELECT * FROM pull_pairs WHERE hash=? AND expires>?",
                    (digest(data.get("pair_code", "")), time.time()),
                ).fetchone()
                if not pair:
                    raise PermissionError("配对码无效或过期")
                node = pair["node"]
                if db.execute(
                    "SELECT 1 FROM pull_jobs WHERE node=? AND state NOT IN ('needs_review','completed','failed','cancelled','timed_out','interrupted')",
                    (node,),
                ).fetchone():
                    raise PermissionError("机器仍有未核对的活动任务；请使用原连接器结束或核对后再配对")
                # A replacement connector must not inherit an unreconciled live lease.
                db.execute(
                    "INSERT INTO pull_devices VALUES(?,?,?) ON CONFLICT(node) DO UPDATE SET token_hash=excluded.token_hash,last_seen=excluded.last_seen",
                    (node, digest(token), time.time()),
                )
                db.execute("DELETE FROM pull_pairs WHERE node=?", (node,))
                self.store.log(db, pair["owner_id"], "device_enrolled", {"node": node})
                return {"node": node, "owner_id": pair["owner_id"]}
        if path == "/api/device/poll":
            return self._poll(data, bearer)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            device = self._device(db, bearer)
            db.execute(
                "UPDATE pull_devices SET last_seen=? WHERE node=?",
                (time.time(), device["node"]),
            )
            if path == "/api/device/projects":
                return {"projects": self._projects(db, device), "node": device["node"]}
            if path in {
                "/api/device/transfers",
                "/api/device/transfer-download-ticket",
                "/api/device/transfer-complete",
            }:
                # FileTransfers owns its own short transactions and filesystem
                # locks. Release the heartbeat write lock before entering it.
                db.commit()
                from agent_harness.file_transfers import FileTransfers

                transfers = FileTransfers(self.store)
                if path == "/api/device/transfers":
                    return {
                        "transfers": transfers.list_for_recipient_node(device["owner_id"], device["node"]),
                        "node": device["node"],
                    }
                transfer_id = data["id"]
                if path == "/api/device/transfer-download-ticket":
                    return transfers.download_ticket(device["owner_id"], device["node"], transfer_id)
                return transfers.complete(device["owner_id"], device["node"], transfer_id)
            if path == "/api/device/owner-report":
                from agent_harness.owner_backend import OwnerBackend

                controls = OwnerBackend.__new__(OwnerBackend)
                controls.store = self.store
                return controls.report(db, device, data["id"], data["state"], data["result"])
            if path == "/api/device/report":
                row = db.execute(
                    "SELECT * FROM pull_jobs WHERE id=?",
                    (data.get("job_id", data.get("job")),),
                ).fetchone()
                if (
                    not row
                    or row["node"] != device["node"]
                    or not hmac.compare_digest(str(data.get("lease_id", "")), row["lease_id"])
                ):
                    raise PermissionError("无效的任务租约")
                state = data.get("state", data.get("status"))
                allowed = {"running", "unknown"} | TERMINAL
                if state not in allowed or row["kind"] == "task" and state == "completed":
                    raise ValueError("无效的任务状态")
                result = data.get("result", {})
                if not isinstance(result, dict):
                    raise ValueError("结果必须为对象")
                result = {
                    k: v
                    for k, v in result.items()
                    if k
                    in {
                        "report",
                        "reply_draft",
                        "error",
                        "local_status",
                        "exit_code",
                        "stdout",
                        "stderr",
                        "started_at",
                        "finished_at",
                        "status",
                        "job",
                        "execution",
                        "requested_model",
                        "reasoning_effort",
                        "effective_model",
                        "model_verified",
                    }
                }
                if "reply_draft" in result and (
                    not isinstance(result["reply_draft"], str) or len(result["reply_draft"]) > 16000
                ):
                    result.pop("reply_draft", None)
                for key in ("report", "error"):
                    if key in data:
                        result[key] = data[key]
                for key in ("requested_model", "effective_model"):
                    if key in result and (not isinstance(result[key], str) or len(result[key]) > 120):
                        raise ValueError("无效的模型审计字段")
                if "reasoning_effort" in result and result["reasoning_effort"] not in {
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                }:
                    raise ValueError("无效的思考强度审计字段")
                if "model_verified" in result and not isinstance(result["model_verified"], bool):
                    raise ValueError("无效的模型核验字段")
                result["status"] = state
                serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
                if len(serialized) > 1000000:
                    raise ValueError("结果过长")
                if row["state"] in TERMINAL:
                    if row["state"] != state or row["result"] != serialized:
                        raise ValueError("已结束的任务结果不可覆写")
                    return {"ok": True, "duplicate": True}
                # A device is responsible for stopping its processes before reporting cancellation.
                db.execute(
                    "UPDATE pull_jobs SET state=?,result=? WHERE id=?",
                    (state, serialized, row["id"]),
                )
                return {"ok": True}
            raise ValueError("未知机器接口")

    def _advertise(self, db, device, bindings):
        if not isinstance(bindings, list) or len(bindings) > 100:
            raise ValueError("无效的本机授权列表")
        prepared = []
        seen = set()
        for b in bindings:
            if not isinstance(b, dict):
                raise ValueError("无效的绑定")
            project = b.get("project_id")
            backend = b.get("backend_project")
            self.store.require_member(db, project, device["owner_id"])
            if (
                not isinstance(backend, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", backend)
                or backend in seen
            ):
                raise ValueError("本机执行项目必须唯一")
            seen.add(backend)
            stages = b.get("stages", ["discuss"])
            templates = b.get("templates", [])
            if (
                not isinstance(stages, list)
                or not set(stages) <= {"discuss", "review", "implement"}
                or not isinstance(templates, list)
                or any(not isinstance(x, str) or len(x) > 120 for x in templates)
            ):
                raise ValueError("无效的本机权限")
            name = b.get("name", backend)
            model = b.get("model", "")
            effort = b.get("effort", "medium")
            timeout = b.get("timeout", 120)
            if (
                not all(isinstance(x, str) and 1 <= len(x) <= 120 for x in (name, model, effort))
                or not isinstance(timeout, int)
                or not 1 <= timeout <= 86400
            ):
                raise ValueError("无效的本机设置摘要")
            old = db.execute(
                "SELECT * FROM agents WHERE node=? AND backend_project=?",
                (device["node"], backend),
            ).fetchone()
            if old and (old["owner_id"] != device["owner_id"] or old["project_id"] != project):
                raise PermissionError("执行项目已经绑定其他工作群；不可复用会话")
            prepared.append(
                (
                    b,
                    old,
                    project,
                    backend,
                    name,
                    model,
                    effort,
                    timeout,
                    stages,
                    templates,
                )
            )
        db.execute("UPDATE agents SET enabled=0 WHERE node=?", (device["node"],))
        for (
            b,
            old,
            project,
            backend,
            name,
            model,
            effort,
            timeout,
            stages,
            templates,
        ) in prepared:
            agent = old["id"] if old else uid("agent")
            enabled = int(bool(b.get("enabled", False)))
            if old:
                db.execute(
                    "UPDATE agents SET name=?,model=?,effort=?,timeout=?,enabled=?,configuring=0 WHERE id=?",
                    (name, model, effort, timeout, enabled, agent),
                )
            else:
                db.execute(
                    "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout,enabled) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        agent,
                        project,
                        name,
                        device["owner_id"],
                        device["node"],
                        backend,
                        model,
                        effort,
                        timeout,
                        enabled,
                    ),
                )
            db.execute(
                "INSERT INTO pull_bindings VALUES(?,?,?) ON CONFLICT(agent_id) DO UPDATE SET stages=excluded.stages,templates=excluded.templates",
                (agent, json.dumps(stages), json.dumps(templates)),
            )

    def _job_binding(self, db, row):
        a = db.execute(
            "SELECT project_id,backend_project FROM agents WHERE id=?",
            (row["agent_id"],),
        ).fetchone()
        if not a:
            raise ValueError("任务绑定不存在")
        return dict(a)

    def _enqueue(self, db, job, agent, kind, request):
        old = db.execute("SELECT * FROM pull_jobs WHERE id=?", (job,)).fetchone()
        serialized = json.dumps(request, ensure_ascii=False, sort_keys=True)
        if old:
            if old["agent_id"] != agent["id"] or old["kind"] != kind or old["request"] != serialized:
                raise ValueError("任务 ID 已用于其他请求")
            return
        if not db.execute("SELECT 1 FROM pull_devices WHERE node=?", (agent["node"],)).fetchone():
            raise ValueError("请先由机器所有者完成本机配对")
        if not agent["enabled"]:
            raise PermissionError("机器所有者未启用本机授权")
        db.execute(
            "INSERT INTO pull_jobs(id,node,agent_id,kind,request,state,lease_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                job,
                agent["node"],
                agent["id"],
                kind,
                serialized,
                "queued",
                secrets.token_urlsafe(24),
                time.time(),
            ),
        )

    def submit(
        self,
        project,
        prompt,
        stage,
        scope,
        acceptance,
        validation,
        job_id=None,
        session_channel=None,
    ):
        context = json.loads(prompt)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = db.execute(
                "SELECT * FROM agents WHERE id=?",
                (context.get("identity", {}).get("id"),),
            ).fetchone()
            if not agent or agent["backend_project"] != project or self.node and agent["node"] != self.node:
                raise PermissionError("无效任务绑定")
            binding = db.execute("SELECT * FROM pull_bindings WHERE agent_id=?", (agent["id"],)).fetchone()
            if not binding or stage not in json.loads(binding["stages"]):
                raise PermissionError("本机所有者未授权这个任务阶段")
            job = job_id or uid("job")
            self._enqueue(
                db,
                job,
                agent,
                "task",
                {
                    "prompt": prompt,
                    "stage": stage,
                    "scope": scope,
                    "acceptance": acceptance,
                    "validation": validation,
                    "session_channel": session_channel or "group",
                },
            )
            return {"job": job, "status": "running"}

    def status(self, job, fetch=False):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM pull_jobs WHERE id=?", (job,)).fetchone()
            if not row or self.node and row["node"] != self.node:
                raise ValueError("未知任务")
            result = json.loads(row["result"] or "{}")
            return {
                **result,
                "job": job,
                "status": "running" if row["state"] in {"queued", "claimed"} else row["state"],
            }

    def rpc(self, data):
        action = data.get("action")
        if action == "execution_start":
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                execution = db.execute(
                    "SELECT e.*,a.node,a.backend_project FROM executions e JOIN agents a ON a.id=e.agent_id WHERE e.id=?",
                    (data.get("execution"),),
                ).fetchone()
                if (
                    not execution
                    or execution["node"] != self.node
                    or execution["backend_project"] != data.get("project")
                    or execution["template"] != data.get("template")
                ):
                    raise PermissionError("无效的执行绑定")
                agent = db.execute("SELECT * FROM agents WHERE id=?", (execution["agent_id"],)).fetchone()
                binding = db.execute(
                    "SELECT templates FROM pull_bindings WHERE agent_id=?",
                    (agent["id"],),
                ).fetchone()
                if not binding or data["template"] not in json.loads(binding["templates"]):
                    raise PermissionError("本机未登记这个命令模板")
                self._enqueue(
                    db,
                    execution["id"],
                    agent,
                    "execution",
                    {"template": data["template"]},
                )
            return {
                "execution": execution["id"],
                "status": "running",
                "local_status": "waiting_local_approval",
            }
        if action in {"cancel", "execution_cancel"}:
            job = data.get("job", data.get("execution"))
            with self.store.connect() as db:
                row = db.execute("SELECT node FROM pull_jobs WHERE id=?", (job,)).fetchone()
                if not row or self.node and row["node"] != self.node:
                    raise PermissionError("未知任务")
                db.execute("UPDATE pull_jobs SET cancel_requested=1 WHERE id=?", (job,))
            return self.status(job)
        if action == "execution_status":
            return self.status(data["execution"])
        if action == "execution_templates":
            if "templates" in data:
                raise PermissionError("命令模板只能在本机控制台设置")
            with self.store.connect() as db:
                rows = db.execute(
                    "SELECT b.templates FROM pull_bindings b JOIN agents a ON a.id=b.agent_id WHERE a.node=? AND a.backend_project=?",
                    (self.node, data.get("project")),
                ).fetchone()
                return {"templates": json.loads(rows["templates"]) if rows else []}
        raise PermissionError("执行和设置只能在机器所有者的本机控制台授权")

    def configure(self, *args, **kwargs):
        raise PermissionError("模型、思考强度和执行路径只能在本机控制台设置")

    def project(self, name):
        raise PermissionError("请在本机控制台接入执行项目")

    def snapshot_nodes(self, user):
        with self.store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT n.name,n.owner_id,d.last_seen,CASE WHEN d.node IS NULL THEN 0 ELSE 1 END AS paired FROM nodes n LEFT JOIN pull_devices d ON d.node=n.name WHERE n.owner_id=?",
                    (user,),
                )
            ]
