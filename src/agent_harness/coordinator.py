"""Event-driven agent turns and independent execution routing. No idle model polling."""

import json
import threading
import time
import logging
import hashlib
from concurrent.futures import ThreadPoolExecutor
from agent_harness.collaboration_store import uid
from agent_harness.broker import PullBackend
from agent_harness.project_memory import ProjectMemory

TERMINAL = {"needs_review", "failed", "cancelled", "timed_out", "interrupted"}


def channel_for(contract):
    partner = contract.get("reply_to") if contract.get("private_turn") else None
    return "dm-" + hashlib.sha256(str(partner).encode()).hexdigest()[:24] if partner else "group"


class Coordinator:
    def __init__(self, store, backend=None):
        self.store = store
        self.memory = ProjectMemory(store)
        self.backend = backend or PullBackend(store)
        self.stop_event = threading.Event()

    def recover(self):
        completed = []
        with self.store.connect() as db:
            starting = db.execute("SELECT * FROM tasks WHERE state='starting'").fetchall()
            for task in starting:
                if isinstance(self.backend, PullBackend):
                    remote = db.execute("SELECT state,result FROM pull_jobs WHERE id=?", (task["job"],)).fetchone()
                    if not remote:
                        db.execute(
                            "UPDATE tasks SET state='failed',processed=0,error=? WHERE id=?",
                            (
                                "Controller stopped before the durable pull job was created; no remote execution was started",
                                task["id"],
                            ),
                        )
                        self.store.emit_task_event(db, task)
                        self.consumed(db, task)
                        completed.append(task["id"])
                        continue
                    result = json.loads(remote["result"] or "{}")
                    state = remote["state"]
                    if state in {"queued", "claimed", "running"}:
                        state = "running"
                    db.execute(
                        "UPDATE tasks SET state=?,report=?,error=?,processed=? WHERE id=?",
                        (
                            state,
                            result.get("report", ""),
                            result.get("error"),
                            0 if state in TERMINAL else task["processed"],
                            task["id"],
                        ),
                    )
                    self.store.emit_task_event(db, task)
                    if state in TERMINAL:
                        self.consumed(db, task)
                        completed.append(task["id"])
                else:
                    db.execute(
                        "UPDATE tasks SET state='unknown',error='Controller restarted during launch; reconcile stored job ID before retry' WHERE id=?",
                        (task["id"],),
                    )
                    self.store.emit_task_event(db, task)
        # Finalization opens its own transaction and must run after the recovery
        # transaction releases the SQLite writer lock.
        for task_id in completed:
            self.process_completed(task_id)

    def consumed(self, db, task):
        channel = channel_for(json.loads(task["contract"]))
        db.execute(
            "INSERT INTO agent_cursors(agent_id,channel,last_message) VALUES(?,?,?) ON CONFLICT(agent_id,channel) DO UPDATE SET last_message=MAX(last_message,excluded.last_message)",
            (task["agent_id"], channel, task["cutoff"]),
        )
        db.execute(
            "UPDATE agents SET last_message=MAX(last_message,?) WHERE id=?",
            (task["cutoff"], task["agent_id"]),
        )

    def agent_context(self, agent, task):
        with self.store.connect() as db:
            project = dict(db.execute("SELECT * FROM projects WHERE id=?", (agent["project_id"],)).fetchone())
            members = [
                {k: r[k] for k in ("id", "name", "owner_id", "model", "effort", "enabled")}
                for r in db.execute("SELECT * FROM agents WHERE project_id=?", (agent["project_id"],))
            ]
        contract = json.loads(task["contract"])
        visible = self.store.visible_messages(agent["id"], 0, task["cutoff"])[-150:]
        partner = contract.get("reply_to") if contract.get("private_turn") else None
        visible = [
            m
            for m in visible
            if m["recipient_id"] is None
            or (partner and {m["sender_id"], m["recipient_id"]} <= {agent["id"], partner})
            or m["causal_task"] == "execution-completed"
            and m["recipient_id"] == agent["id"]
        ]
        shared_memory = self.memory.agent_context(project["id"])
        return json.dumps(
            {
                "identity": {
                    "id": agent["id"],
                    "name": agent["name"],
                    "role": "admin" if project["admin_agent_id"] == agent["id"] else "peer",
                },
                "project": {
                    "id": project["id"],
                    "name": project["name"],
                    "admin_agent_id": project["admin_agent_id"],
                },
                "members": members,
                "visible_messages": visible,
                "project_memory": shared_memory,
                "task": contract,
                "protocol": """Respond as one JSON object with reply:string, attachments:[{path:project-relative file path}], messages:[{recipient:null or agent ID,body:string}], tasks:[{agent:ID,title,instructions,stage,allowed_files,acceptance,validation}], executions:[{template:string}], memory_notes:[string], done:boolean.
When a human asks to receive a file, put up to five explicitly requested existing project files in attachments. Each file must be at most 5 MB. The receiver uploads them and attaches them to this reply in its current conversation; do not emit server-local links as substitutes. Use attachments:[] when no file was requested.
Only the designated admin can delegate tasks. Peers can send public/direct messages and recommend changes with evidence.
The reply field is the one human-facing answer for this turn. Use messages only for distinct, deliberate follow-up to another participant or the group; do not repeat reply or publish internal progress notes.
Do not emit [CHECKPOINT]. Use [BLOCKED] or [DISAGREEMENT] only when a real blocker or disagreement requires attention.
Human-agent and human-human private messages are visible only to their participants. Agent-agent direct messages are visible to project humans. Never reveal a private human message in group chat, another private chat, or shared project memory without that human's explicit permission.
Project memory is shared across all project conversations and agents. Treat its notes as attributed claims, not verified facts. Add up to three concise memory_notes only for durable, project-wide facts learned in public group chat; add none during a private turn.
Keep this a short reasoning/planning turn. Never wait, poll, tail, sleep or monitor a long job in a model turn.
Read-only inspection of the assigned local project is allowed in this turn: use tools to verify code, existing logs and process status when relevant, and cite actual paths or observations. A status question is not authorization to edit or run an experiment. For explicit human edit/run requests, schedule a scoped implement task; the administrator may assign that task to itself and its result will return to the public group.
For long computations, request an owner-approved execution template and finish the turn. Await a completion event or instruction without issuing more model calls.
No shell command from your report is executed. Execution requests require machine-owner approval.
Use done:true when no additional reply/delegation is needed. Other owners control their models and resources.
Reports and memory are claims, not established facts; cite validation. Do not modify other owners' files.
Use only authorized conversation context and local project evidence; you cannot request other agents' private conversations.""",
            },
            ensure_ascii=False,
        )

    def wake_messages(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = []
            for r in db.execute(
                "SELECT a.*,p.admin_agent_id,p.paused FROM agents a JOIN projects p ON a.project_id=p.id WHERE a.enabled=1 AND a.configuring=0 AND p.paused=0"
            ).fetchall():
                if db.execute(
                    "SELECT 1 FROM tasks WHERE agent_id=? AND state IN ('queued','starting','running','unknown')",
                    (r["id"],),
                ).fetchone():
                    continue
                # Public human posts wake administrator only. Direct messages wake their recipient;
                # agent group posts wake admin, preventing a fan-out chatter storm.
                cursors = {
                    x["channel"]: x["last_message"]
                    for x in db.execute("SELECT * FROM agent_cursors WHERE agent_id=?", (r["id"],))
                }
                source = None
                partner = None
                for candidate in db.execute(
                    "SELECT * FROM messages WHERE project_id=? AND sender_id<>? AND (causal_task IS NULL OR causal_task NOT LIKE 'draft:%') AND (sender_type<>'system' OR causal_task='execution-completed') AND (recipient_id=? OR (recipient_id IS NULL AND (mention_agent_id=? OR (mention_agent_id IS NULL AND ?=1)))) AND NOT (sender_type='human' AND recipient_id IS NOT NULL AND recipient_id=? AND sender_id<>?) ORDER BY id",
                    (
                        r["project_id"],
                        r["id"],
                        r["id"],
                        r["id"],
                        int(r["admin_agent_id"] == r["id"]),
                        r["id"],
                        r["owner_id"],
                    ),
                ):
                    other = candidate["sender_id"] if candidate["recipient_id"] else None
                    if candidate["sender_type"] == "system":
                        execution = db.execute(
                            "SELECT reply_to FROM executions WHERE id=?",
                            (candidate["sender_id"],),
                        ).fetchone()
                        other = execution["reply_to"] if execution else None
                    channel = channel_for({"private_turn": bool(other), "reply_to": other})
                    if candidate["id"] > cursors.get(channel, 0):
                        source, partner = candidate, other
                        break
                if not source:
                    continue
                # Addressed messages take priority over general group chatter.
                pending.append(
                    (
                        0 if source["recipient_id"] or source["mention_agent_id"] else 1,
                        source["id"],
                        dict(r),
                        partner,
                    )
                )
            for _, _, r, partner in sorted(pending, key=lambda item: (item[0], item[1])):
                project = dict(db.execute("SELECT * FROM projects WHERE id=?", (r["project_id"],)).fetchone())
                if project["paused"]:
                    continue
                private = bool(partner)
                data = {
                    "title": "处理新消息",
                    "instructions": "处理收到的可见消息；管理员根据目标协调分工，必要时向同事定向发送消息或派任务。若无需回复，返回 done:true。",
                    "stage": "discuss",
                    "allowed_files": "允许只读检查员工配置的项目目录、代码、已有日志与进程状态，可运行查看和搜索命令；本轮不得修改文件、安装软件或启动实验。人类明确要求修改或运行实验时，管理员应另行分派 implement 任务，写明文件范围、验收与验证。",
                    "acceptance": "回复针对消息且遵守收件权限，任务分工明确",
                    "validation": "核对可见消息与任务契约；不要编造执行结果",
                }
                if private:
                    data.update(private_turn=True, reply_to=partner)
                self.store._task(db, project, dict(r), data, "scheduler")

    def dispatch(self, task_id=None):
        with self.store.connect() as db:
            tasks = [
                dict(x)
                for x in db.execute(
                    "SELECT t.* FROM tasks t JOIN projects p ON t.project_id=p.id JOIN agents a ON t.agent_id=a.id WHERE t.state='queued' AND p.paused=0 AND a.enabled=1 AND a.configuring=0"
                )
            ]
        for task in tasks:
            if task_id and task["id"] != task_id:
                continue
            job = uid("job")
            with self.store.connect() as db:
                agent = dict(db.execute("SELECT * FROM agents WHERE id=?", (task["agent_id"],)).fetchone())
                if (
                    db.execute(
                        "UPDATE tasks SET state='starting',job=? WHERE id=? AND state='queued'",
                        (job, task["id"]),
                    ).rowcount
                    != 1
                ):
                    continue
                self.store.emit_task_event(db, task)
            contract = json.loads(task["contract"])
            try:
                channel = channel_for(contract)
                kwargs = (
                    {"job_id": job, "session_channel": channel} if getattr(self.backend, "durable_ids", False) else {}
                )
                result = self.backend.submit(
                    agent["backend_project"],
                    self.agent_context(agent, task),
                    contract.get("stage", "discuss"),
                    contract.get("allowed_files", ""),
                    contract["acceptance"],
                    contract["validation"],
                    **kwargs,
                )
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE tasks SET state='running',job=? WHERE id=?",
                        (result["job"], task["id"]),
                    )
                    self.store.emit_task_event(db, task)
            except Exception as exc:
                # Submission is idempotent and records remote uncertainty; do not blindly resubmit.
                text = str(exc)
                uncertain = "not confirmed" in text.lower()
                job = None
                if uncertain:
                    import re

                    match = re.search(r"job-[a-f0-9]{32}", text)
                    job = match.group(0) if match else None
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE tasks SET state=?,job=?,error=?,processed=1 WHERE id=?",
                        (
                            "unknown" if uncertain else "failed",
                            job,
                            text[:4000],
                            task["id"],
                        ),
                    )
                    self.store.emit_task_event(db, task)
                    self.store.clear_draft(db, task["id"])
                    self.consumed(db, task)
                    self.store._message(
                        db,
                        task["project_id"],
                        "system",
                        "system",
                        "任务启动未确认，请人工核对，系统不会自动重试。"
                        if uncertain
                        else "任务启动失败：" + text[:1000],
                        agent["id"],
                        task["id"],
                    )

    def collect(self, task_id=None):
        with self.store.connect() as db:
            tasks = [
                dict(x)
                for x in db.execute("SELECT * FROM tasks WHERE state IN ('running','unknown') AND job IS NOT NULL")
            ]
        for task in tasks:
            if task_id and task["id"] != task_id:
                continue
            try:
                if task.get("cancel_requested"):
                    self.backend.rpc({"action": "cancel", "job": task["job"]})
                result = self.backend.status(task["job"], fetch=True)
            except Exception:
                continue
            draft = result.get("reply_draft")
            if isinstance(draft, str) and len(draft) <= 16000:
                self.update_reply_draft(task, draft)
            if result.get("status") not in TERMINAL:
                continue
            with self.store.connect() as db:
                db.execute(
                    "UPDATE tasks SET state=?,report=?,error=?,processed=0 WHERE id=?",
                    (
                        result["status"],
                        result.get("report", ""),
                        result.get("error"),
                        task["id"],
                    ),
                )
                self.store.emit_task_event(db, task)
                self.consumed(db, task)
            self.process_completed(task["id"])

    def update_reply_draft(self, task, draft):
        with self.store.connect() as db:
            agent = db.execute("SELECT * FROM agents WHERE id=?", (task["agent_id"],)).fetchone()
            project = db.execute("SELECT * FROM projects WHERE id=?", (task["project_id"],)).fetchone()
            if not agent or not project:
                return
            contract = json.loads(task["contract"])
            private = bool(contract.get("private_turn"))
            recipient = contract.get("reply_to") if private else None
            self.store.draft_message(db, project["id"], agent["id"], draft, recipient, task["id"])

    def process_completed(self, task_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["processed"]:
                return
            task = dict(row)
            agent = dict(db.execute("SELECT * FROM agents WHERE id=?", (task["agent_id"],)).fetchone())
            project = dict(db.execute("SELECT * FROM projects WHERE id=?", (task["project_id"],)).fetchone())
            db.execute("UPDATE tasks SET processed=1 WHERE id=?", (task_id,))
            original = json.loads(task["contract"])
            reply_to = original.get("reply_to")
            private = bool(original.get("private_turn"))
            if task["state"] != "needs_review":
                answer = None
                try:
                    candidate = json.loads(task["report"])
                    if isinstance(candidate, dict) and isinstance(candidate.get("reply", ""), str):
                        answer = candidate
                except (ValueError, TypeError):
                    pass
                reply = (answer or {}).get("reply", "").strip()
                if reply.casefold().startswith("[checkpoint]"):
                    reply = ""
                if reply:
                    if not self.store.finalize_draft(db, task_id, reply, reply_to):
                        self.store._message(
                            db,
                            project["id"],
                            agent["id"],
                            "agent",
                            reply,
                            reply_to,
                            task_id,
                        )
                    notice = (
                        agent["name"]
                        + " 已生成回复，但本机执行状态为 "
                        + task["state"]
                        + "；本轮未执行派工、记忆或后台请求。"
                    )
                else:
                    self.store.clear_draft(db, task_id)
                    notice = (
                        agent["name"]
                        + " 未能回复：本机执行状态为 "
                        + task["state"]
                        + "。请由机器所有者检查 Codex、客户端版本和超时设置。"
                    )
                self.store._message(
                    db,
                    project["id"],
                    "system",
                    "system",
                    notice,
                    reply_to if private else None,
                    task_id,
                )
                return
            try:
                answer = json.loads(task["report"])
                if not isinstance(answer, dict) or not isinstance(answer.get("reply", ""), str):
                    raise ValueError("Invalid structured response")
                for key in ("messages", "tasks", "executions", "memory_notes"):
                    if not isinstance(answer.get(key, []), list) or len(answer.get(key, [])) > 10:
                        raise ValueError("Invalid action list")
            except Exception:
                # Plain reports remain auditable; never interpret commands or guessed JSON fragments.
                self.store.clear_draft(db, task_id)
                self.store._message(
                    db,
                    project["id"],
                    agent["id"],
                    "agent",
                    task["report"][:16000] or "无报告",
                    reply_to
                    if private
                    else (project["admin_agent_id"] if project["admin_agent_id"] != agent["id"] else None),
                    task_id,
                )
                return
            reply = answer.get("reply", "").strip()
            if reply.casefold().startswith("[checkpoint]"):
                self.store.log(db, "scheduler", "suppressed_checkpoint", {"task": task_id})
                reply = ""
            from agent_harness.attachments import Attachments

            files = Attachments(self.store)
            try:
                attachment_ids = files.validate_agent_reply(
                    db, agent, task.get("job") or "", answer.get("attachment_ids", [])
                )
            except (ValueError, TypeError, PermissionError):
                attachment_ids = []
                reply = reply[:15500] + "\n\n附件未发送：当前任务的附件验证失败。"
                self.store.log(db, "scheduler", "rejected_agent_attachments", {"task": task_id})
            if attachment_ids and not reply:
                reply = "已发送附件。"
            if reply:
                if not self.store.finalize_draft(db, task_id, reply, reply_to):
                    self.store._message(
                        db,
                        project["id"],
                        agent["id"],
                        "agent",
                        reply,
                        reply_to,
                        task_id,
                    )
                if attachment_ids:
                    message = db.execute(
                        "SELECT id FROM messages WHERE causal_task=? AND sender_id=? ORDER BY id DESC LIMIT 1",
                        (task_id, agent["id"]),
                    ).fetchone()
                    files.bind(db, agent["id"], project["id"], message["id"], attachment_ids)
            else:
                self.store.clear_draft(db, task_id)
            if not private and answer.get("memory_notes"):
                try:
                    self.memory.add_agent_notes(db, agent["id"], project["id"], task_id, answer["memory_notes"])
                except ValueError:
                    self.store.log(db, "scheduler", "rejected_memory_notes", {"task": task_id})
            for message in answer.get("messages", []):
                if not isinstance(message, dict):
                    continue
                try:
                    if private and message.get("recipient") not in {
                        reply_to,
                        agent["id"],
                    }:
                        raise ValueError("Private turn cannot broadcast or forward to other agents")
                    self.store._message(
                        db,
                        project["id"],
                        agent["id"],
                        "agent",
                        message["body"],
                        message.get("recipient"),
                        task_id,
                    )
                except (ValueError, KeyError, TypeError):
                    self.store.log(db, "scheduler", "rejected_message", {"task": task_id})
            if project["admin_agent_id"] == agent["id"] and not private:
                for assignment in answer.get("tasks", []):
                    db.execute("SAVEPOINT delegation")
                    try:
                        target = db.execute(
                            "SELECT * FROM agents WHERE id=? AND project_id=? AND enabled=1",
                            (assignment["agent"], project["id"]),
                        ).fetchone()
                        if not target:
                            raise ValueError("Target not in project")
                        # A self-assignment continues this public request and
                        # must report back to the human, not to a private
                        # conversation addressed to the very same agent.
                        same_agent = target["id"] == agent["id"]
                        assignment["reply_to"] = None if same_agent else agent["id"]
                        assignment["private_turn"] = not same_agent
                        if not same_agent:
                            self.store._message(
                                db,
                                project["id"],
                                agent["id"],
                                "agent",
                                "分派任务：" + assignment["title"],
                                target["id"],
                                task_id,
                            )
                        self.store._task(
                            db,
                            dict(
                                db.execute(
                                    "SELECT * FROM projects WHERE id=?",
                                    (project["id"],),
                                ).fetchone()
                            ),
                            dict(target),
                            assignment,
                            agent["id"],
                        )
                        db.execute("RELEASE delegation")
                    except Exception as exc:
                        db.execute("ROLLBACK TO delegation")
                        db.execute("RELEASE delegation")
                        self.store.log(
                            db,
                            "scheduler",
                            "rejected_delegation",
                            {"task": task_id, "reason": str(exc)[:500]},
                        )
            elif answer.get("tasks"):
                self.store.log(db, "scheduler", "rejected_nonadmin_delegation", {"task": task_id})
            for request in answer.get("executions", []):
                template = request.get("template") if isinstance(request, dict) else None
                if not isinstance(template, str) or not 1 <= len(template) <= 64:
                    continue
                execution = uid("execution")
                db.execute(
                    "INSERT INTO executions(id,project_id,agent_id,template,state,created_at,reply_to) VALUES(?,?,?,?,?,?,?)",
                    (
                        execution,
                        project["id"],
                        agent["id"],
                        template,
                        "awaiting_owner",
                        time.time(),
                        reply_to if private else None,
                    ),
                )
                execution_row = db.execute("SELECT * FROM executions WHERE id=?", (execution,)).fetchone()
                self.store.emit_execution_event(db, execution_row)
                self.store.log(
                    db,
                    agent["id"],
                    "execution_requested",
                    {"execution": execution, "template": template},
                )

    def executions(self, execution_id=None):
        with self.store.connect() as db:
            rows = [
                dict(x)
                for x in db.execute(
                    "SELECT e.*,a.node,a.backend_project FROM executions e JOIN agents a ON e.agent_id=a.id WHERE e.state IN ('approved','starting','running','unknown')"
                )
            ]
        for row in rows:
            if execution_id and row["id"] != execution_id:
                continue
            backend = self.backend.for_node(row["node"]) if hasattr(self.backend, "for_node") else self.backend
            try:
                if row["state"] == "approved":
                    with self.store.connect() as db:
                        if (
                            db.execute(
                                "UPDATE executions SET state='starting' WHERE id=? AND state='approved'",
                                (row["id"],),
                            ).rowcount
                            != 1
                        ):
                            continue
                        self.store.emit_execution_event(db, row)
                    result = backend.rpc(
                        {
                            "action": "execution_start",
                            "project": row["backend_project"],
                            "execution": row["id"],
                            "template": row["template"],
                        }
                    )
                else:
                    result = backend.rpc(
                        {
                            "action": "execution_cancel" if row.get("cancel_requested") else "execution_status",
                            "execution": row["id"],
                        }
                    )
                state = result.get("status", "unknown")
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE executions SET state=?,result=? WHERE id=?",
                        (state, json.dumps(result, ensure_ascii=False), row["id"]),
                    )
                    self.store.emit_execution_event(db, row)
                    if (
                        state
                        in {
                            "completed",
                            "failed",
                            "cancelled",
                            "timed_out",
                            "interrupted",
                        }
                        and not row["notified"]
                    ):
                        self.store._message(
                            db,
                            row["project_id"],
                            row["id"],
                            "system",
                            "后台作业 "
                            + row["id"]
                            + " 已结束："
                            + state
                            + "。结果："
                            + json.dumps(result, ensure_ascii=False)[-6000:],
                            row["agent_id"],
                            "execution-completed",
                        )
                        db.execute("UPDATE executions SET notified=1 WHERE id=?", (row["id"],))
            except Exception as exc:
                with self.store.connect() as db:
                    # RPC may have succeeded remotely. Record unknown and query same ID later.
                    db.execute(
                        "UPDATE executions SET state=?,result=? WHERE id=?",
                        ("unknown", json.dumps({"error": str(exc)[:2000]}), row["id"]),
                    )
                    self.store.emit_execution_event(db, row)

    def tick(self):
        self.collect()
        self.executions()
        self.wake_messages()
        self.dispatch()

    def run(self):
        self.recover()
        # Lightweight service polls process state only while jobs are active. No model requests at idle.
        pending = {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="node-rpc") as pool:
            while not self.stop_event.is_set():
                try:
                    for key, future in list(pending.items()):
                        if future.done():
                            del pending[key]
                            try:
                                future.result()
                            except Exception:
                                logging.exception("Node operation failed")
                    self.wake_messages()
                    with self.store.connect() as db:
                        tasks = [
                            dict(x)
                            for x in db.execute(
                                "SELECT id,state,processed FROM tasks WHERE state IN ('queued','running','unknown') OR processed=0 AND state IN ('needs_review','failed','cancelled','timed_out','interrupted')"
                            )
                        ]
                        executions = [
                            r["id"]
                            for r in db.execute(
                                "SELECT id FROM executions WHERE state IN ('approved','starting','running','unknown')"
                            )
                        ]
                    for task in tasks:
                        key = "task:" + task["id"]
                        if key in pending:
                            continue
                        method = (
                            self.dispatch
                            if task["state"] == "queued"
                            else self.collect
                            if task["state"] in {"running", "unknown"}
                            else self.process_completed
                        )
                        pending[key] = pool.submit(method, task["id"])
                    for execution in executions:
                        key = "execution:" + execution
                        if key not in pending:
                            pending[key] = pool.submit(self.executions, execution)
                except Exception:
                    logging.exception("Coordinator tick failed")
                self.stop_event.wait(1)
