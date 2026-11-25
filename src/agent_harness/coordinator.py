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

# Who may read the result of a task delegated by an agent. "delegator" keeps the
# existing private behaviour; "group" publishes the report to the project group;
# "owner" delivers it to the executing agent's own owner.
AUDIENCES = {"delegator", "group", "owner"}


def channel_for(contract):
    partner = contract.get("reply_to") if contract.get("private_turn") else None
    return "dm-" + hashlib.sha256(str(partner).encode()).hexdigest()[:24] if partner else "group"


class Coordinator:
    def __init__(self, store, backend=None):
        self.store = store
        self.memory = ProjectMemory(store)
        self.backend = backend or PullBackend(store)
        self.stop_event = threading.Event()

    def _work(self):
        # Imported lazily so older databases can still be inspected while an
        # upgrade is being installed.
        from agent_harness.project_work import ProjectWork

        return ProjectWork(self.store)

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
        trace = db.execute(
            "SELECT wake_source_id,parent_task_id FROM task_trace WHERE task_id=?", (task["id"],)
        ).fetchone()
        # A formal assignment already has a task. Its notice does not consume queued DMs.
        if trace and trace["parent_task_id"] is not None:
            return
        # Older in-flight tasks already reserved their cutoff before this upgrade.
        message_id = trace["wake_source_id"] if trace else task["cutoff"]
        if message_id is None:
            return
        channel = channel_for(json.loads(task["contract"]))
        db.execute(
            "INSERT INTO agent_cursors(agent_id,channel,last_message) VALUES(?,?,?) ON CONFLICT(agent_id,channel) DO UPDATE SET last_message=MAX(last_message,excluded.last_message)",
            (task["agent_id"], channel, message_id),
        )
        db.execute(
            "UPDATE agents SET last_message=MAX(last_message,?) WHERE id=?",
            (message_id, task["agent_id"]),
        )

    def agent_context(self, agent, task):
        with self.store.connect() as db:
            project = dict(db.execute("SELECT * FROM projects WHERE id=?", (agent["project_id"],)).fetchone())
            members = [
                {k: r[k] for k in ("id", "name", "node", "owner_id", "model", "effort", "enabled")}
                for r in db.execute("SELECT * FROM agents WHERE project_id=?", (agent["project_id"],))
            ]
            trace = db.execute("SELECT source_message_id FROM task_trace WHERE task_id=?", (task.get("id"),)).fetchone()
            approval = db.execute(
                "SELECT id,source_task_id,reason FROM permission_requests WHERE continuation_task_id=? "
                "AND project_id=? AND agent_id=? AND owner_id=? AND state IN ('approved','consumed')",
                (task.get("id"), project["id"], agent["id"], agent["owner_id"]),
            ).fetchone()
        contract = json.loads(task["contract"])
        work_execution = contract.get("project_work_execution_id")
        if work_execution:
            with self.store.connect() as db:
                work_context = self._work().execution_context(db, work_execution)
            if not work_context:
                raise ValueError("Scheduled project work has no authorized execution context")
            # Work execution gets a deliberately narrow prompt. In particular it
            # cannot inherit an agent's private/group history or project memory.
            return json.dumps(
                {
                    "identity": {
                        "id": agent["id"],
                        "name": agent["name"],
                        "node": agent["node"],
                        "role": "admin" if project["admin_agent_id"] == agent["id"] else "peer",
                    },
                    "project": {"id": project["id"], "name": project["name"]},
                    "task": contract,
                    "project_work": work_context,
                    "grants": ["full_access"] if approval else [],
                    **(
                        {"permission_request_id": approval["id"], "approved_continuation": dict(approval)}
                        if approval
                        else {}
                    ),
                    "protocol": (
                        "Execute only this scheduled project work. Use work_delivery for completion; "
                        "do not send unrelated messages, delegate tasks, or request background executions."
                    ),
                },
                ensure_ascii=False,
            )
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
        if trace and any(message["id"] == trace["source_message_id"] for message in visible):
            contract["source_message_id"] = trace["source_message_id"]
        drafts = []
        existing_work = []
        if project["admin_agent_id"] == agent["id"] and trace and trace["source_message_id"]:
            with self.store.connect() as db:
                source = db.execute("SELECT * FROM messages WHERE id=?", (trace["source_message_id"],)).fetchone()
                if source and source["sender_type"] == "human":
                    drafts = self._work().agent_drafts(db, project, agent["id"], dict(source))
                    existing_work = self._work().agent_work(db, project, agent["id"], dict(source))
        shared_memory = self.memory.agent_context(project["id"])
        return json.dumps(
            {
                "identity": {
                    "id": agent["id"],
                    "name": agent["name"],
                    "node": agent["node"],
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
                "project_work_drafts": drafts,
                "project_work_items": existing_work,
                "task": contract,
                "grants": ["full_access"] if approval else [],
                **(
                    {"permission_request_id": approval["id"], "approved_continuation": dict(approval)}
                    if approval
                    else {}
                ),
                # Files the humans attached to the message that woke this turn.
                "attachments": contract.get("attachments") or [],
                # The behavioural rules live once, in the worker prompt; the
                # per-turn contract carries only what changes.
                "protocol": (
                    "Delegation is admin-only; a task carries audience delegator (private report back to you), "
                    "group (published to the project) or owner (to the executor's owner). Model capabilities are yours to use."
                    + (
                        " As project administrator in this human-directed turn, you may return a complete work_proposal "
                        "for the human to review. This only saves or revises a draft: never claim it starts work, and "
                        "ask the human to confirm the exact draft and revision in the desktop UI."
                        if project["admin_agent_id"] == agent["id"]
                        else ""
                    )
                    + (
                        " Only continue the approved source task using the one-time grant for this turn."
                        if approval
                        else ""
                    )
                ),
            },
            ensure_ascii=False,
        )

    def wake_messages(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = []
            for r in db.execute(
                "SELECT a.* FROM agents a JOIN projects p ON a.project_id=p.id WHERE a.enabled=1 AND a.configuring=0 AND p.paused=0"
            ).fetchall():
                if db.execute(
                    "SELECT 1 FROM tasks WHERE agent_id=? AND state IN ('queued','starting','running','unknown')",
                    (r["id"],),
                ).fetchone():
                    continue
                # Only explicit addressees wake. Ordinary group messages are context.
                cursors = {
                    x["channel"]: x["last_message"]
                    for x in db.execute("SELECT * FROM agent_cursors WHERE agent_id=?", (r["id"],))
                }
                source = None
                partner = None
                for candidate in db.execute(
                    "SELECT * FROM messages WHERE project_id=? AND sender_id<>? "
                    "AND (causal_task IS NULL OR causal_task NOT LIKE 'draft:%') "
                    "AND (sender_type<>'system' OR causal_task='execution-completed') "
                    "AND (recipient_id=? OR (recipient_id IS NULL AND mention_agent_id=?)) "
                    "AND NOT (sender_type='human' AND recipient_id IS NOT NULL AND recipient_id=? AND sender_id<>?) "
                    "AND NOT EXISTS (SELECT 1 FROM task_trace r JOIN tasks t ON t.id=r.task_id "
                    "WHERE r.wake_source_id=messages.id AND r.parent_task_id IS NOT NULL AND t.agent_id=?) "
                    "ORDER BY id",
                    (
                        r["project_id"],
                        r["id"],
                        r["id"],
                        r["id"],
                        r["id"],
                        r["owner_id"],
                        r["id"],
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
                pending.append((source["id"], dict(r), partner))
            for source_id, r, partner in sorted(pending, key=lambda item: item[0]):
                project = dict(db.execute("SELECT * FROM projects WHERE id=?", (r["project_id"],)).fetchone())
                if project["paused"]:
                    continue
                private = bool(partner)
                data = {
                    "title": "处理新消息",
                    "instructions": "处理收到的可见消息；管理员根据目标协调分工，必要时向同事定向发送消息或派任务。若无需回复或后续行动，请将 reply 留空并结束本轮。",
                    "stage": "discuss",
                    "allowed_files": "可以使用项目 Agent 的工作目录与执行工具；写入项目外路径需由机器主人批准一次续跑。人类明确要求实现或跑实验时，管理员应分派 implement 任务，写明文件范围、验收与验证。",
                    "acceptance": "回复针对消息且遵守收件权限，任务分工明确",
                    "validation": "核对可见消息与任务契约；不要编造执行结果",
                }
                from agent_harness.attachments import Attachments

                files = Attachments(self.store).metadata(db, source_id)
                if files:
                    data["attachments"] = [
                        {key: item.get(key) for key in ("id", "name", "mime", "size")} for item in files
                    ]
                if private:
                    data.update(private_turn=True, reply_to=partner)
                self.store._task(
                    db,
                    project,
                    r,
                    data,
                    "scheduler",
                    cutoff=source_id,
                    source_message_id=source_id,
                    wake_source_id=source_id,
                )

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
                work_execution = contract.get("project_work_execution_id")
                if work_execution:
                    with self.store.connect() as db:
                        work_context = self._work().execution_context(db, work_execution)
                    if not work_context:
                        raise ValueError("Scheduled project work has no authorized execution context")
                    work_id = work_context["work_id"]
                    channel = work_id if work_id.startswith("work-") else "work-" + work_id
                else:
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
                    if work_execution and not uncertain:
                        self._work().finish(db, work_execution, "blocked")

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
            state = result.get("status")
            if state == "running" and result.get("error") == "Unknown job":
                # Existing hubs may already have stored a report from an older receiver.
                state = "unknown"
            if state not in TERMINAL:
                if state in {"running", "unknown"} and state != task["state"]:
                    with self.store.connect() as db:
                        if db.execute(
                            "UPDATE tasks SET state=?,error=? WHERE id=? AND state IN ('running','unknown')",
                            (state, result.get("error") if state == "unknown" else None, task["id"]),
                        ).rowcount:
                            self.store.emit_task_event(db, task)
                continue
            with self.store.connect() as db:
                db.execute(
                    "UPDATE tasks SET state=?,report=?,error=?,processed=0,cancel_requested=0 WHERE id=?",
                    (
                        state,
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

    def _publish_reply(self, db, task, reply, recipient):
        draft = db.execute("SELECT id FROM messages WHERE causal_task=?", ("draft:" + task["id"],)).fetchone()
        if draft and recipient:
            channel = channel_for({"private_turn": True, "reply_to": task["agent_id"]})
            already_seen = db.execute(
                "SELECT 1 FROM agent_cursors WHERE agent_id=? AND channel=? AND last_message>=?",
                (recipient, channel, draft["id"]),
            ).fetchone()
            reserved = db.execute(
                "SELECT 1 FROM tasks t LEFT JOIN task_trace r ON r.task_id=t.id WHERE t.agent_id=? "
                "AND t.state IN ('queued','starting','running','unknown') "
                "AND json_extract(t.contract,'$.private_turn')=1 AND json_extract(t.contract,'$.reply_to')=? "
                "AND (CASE WHEN r.task_id IS NULL THEN t.cutoff "
                "WHEN r.parent_task_id IS NULL THEN r.wake_source_id END)>=?",
                (recipient, task["agent_id"], draft["id"]),
            ).fetchone()
            if already_seen or reserved:
                # Keep late directed results schedulable even if their draft predates the cursor.
                self.store.clear_draft(db, task["id"])
                draft = None
        if draft and self.store.finalize_draft(db, task["id"], reply, recipient):
            return draft["id"]
        return self.store._message(db, task["project_id"], task["agent_id"], "agent", reply, recipient, task["id"])[
            "id"
        ]

    def _notify_delegator(self, db, task, contract, reply_to, message_id=None):
        target = contract.get("reply_to") if contract.get("private_turn") else None
        if (
            not target
            or target in {reply_to, task["agent_id"]}
            or not db.execute(
                "SELECT 1 FROM agents WHERE id=? AND project_id=?", (target, task["project_id"])
            ).fetchone()
        ):
            return
        if message_id is not None:
            note = f"交办任务 {task['id']} 的结果已发布到群聊（消息 #{message_id}）。"
        else:
            note = f"交办任务 {task['id']} 已结束，执行状态为 {task['state']}。"
        self.store._message(db, task["project_id"], task["agent_id"], "agent", note, target, task["id"])

    def _apply_admin_work_action(self, db, task, project, agent, proposal, follow_up):
        trace = db.execute("SELECT wake_source_id FROM task_trace WHERE task_id=?", (task["id"],)).fetchone()
        source = (
            db.execute("SELECT * FROM messages WHERE id=?", (trace["wake_source_id"],)).fetchone()
            if trace and trace["wake_source_id"]
            else None
        )
        try:
            if project["admin_agent_id"] != agent["id"] or not source or source["sender_type"] != "human":
                raise PermissionError("此轮没有来自人类的定向任务请求")
            if proposal is not None and follow_up is not None:
                raise ValueError("一次回复只能整理一项草稿或补充要求")
            if follow_up is not None:
                if not isinstance(follow_up, dict) or set(follow_up) != {"work_id"}:
                    raise ValueError("补充要求缺少明确的任务编号")
                work = self._work().propose_follow_up(db, project, agent, dict(source), follow_up["work_id"])
                return True, f"这条要求已补充到「{work['title']}」。"
            draft = self._work().propose(db, project, agent, dict(source), proposal)
            return True, f"「{draft['title']}」草稿已保存为第 {draft['revision']} 版，请核对后确认创建。"
        except (ValueError, TypeError, PermissionError) as exc:
            self.store.log(
                db, "scheduler", "rejected_project_work_action", {"task": task["id"], "reason": str(exc)[:500]}
            )
            return False, "这次建议未保存。请核对任务或草稿版本后再告诉我。"

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
            work_execution = original.get("project_work_execution_id")
            reply_to = self.store.task_reply_recipient(agent, original)
            private = bool(original.get("private_turn"))
            # Where this turn's final report lands. A task delegated by an agent
            # comes back to the delegator by default; the task itself may ask for
            # the report to go to the group or to the executing agent's owner.
            audience = original.get("audience") if original.get("audience") in AUDIENCES else "delegator"
            group_posts = 0
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
                if task["state"] == "failed":
                    self.store.post_permission_request(db, project["id"], agent, task, (answer or {}).get("request"))
                if reply:
                    self._publish_reply(db, task, reply, reply_to)
                    notice = (
                        agent["name"]
                        + " 已生成回复，但本机执行状态为 "
                        + task["state"]
                        + "；本轮未执行派工、记忆或后台请求。"
                    )
                else:
                    self.store.clear_draft(db, task_id)
                    if task["state"] == "cancelled":
                        notice = agent["name"] + " 的任务已取消。"
                    else:
                        notice = (
                            agent["name"]
                            + " 未能回复：本机执行状态为 "
                            + task["state"]
                            + "。请由机器所有者检查本机 Agent（Codex 或 DeepSeek Harness）、客户端版本和超时设置。"
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
                formal_handoff = db.execute(
                    "SELECT 1 FROM task_trace WHERE task_id=? AND parent_task_id IS NOT NULL", (task_id,)
                ).fetchone()
                if formal_handoff:
                    self._notify_delegator(db, task, original, reply_to if reply else None)
                if work_execution:
                    self._work().finish(db, work_execution, "waiting" if (answer or {}).get("request") else "blocked")
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
                message_id = self._publish_reply(db, task, (task["report"] or "")[:16000] or "无报告", reply_to)
                if private and audience == "group":
                    self._notify_delegator(db, task, original, reply_to, message_id)
                if work_execution:
                    self._work().finish(db, work_execution, "blocked")
                return
            reply = answer.get("reply", "").strip()
            if work_execution and not reply and isinstance(answer.get("work_delivery"), dict):
                reply = str(answer["work_delivery"].get("summary") or "").strip()
            if reply.casefold().startswith("[checkpoint]"):
                self.store.log(db, "scheduler", "suppressed_checkpoint", {"task": task_id})
                reply = ""
            proposal = answer.get("work_proposal")
            follow_up = answer.get("work_follow_up")
            if not work_execution and (proposal is not None or follow_up is not None):
                saved, notice = self._apply_admin_work_action(db, task, project, agent, proposal, follow_up)
                body = reply[:15000] + "\n\n" + notice if saved and reply else notice
                self._publish_reply(db, task, body, reply_to)
                return
            self.store.post_permission_request(db, project["id"], agent, task, answer.get("request"))
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
            delivery = answer.get("work_delivery")
            if (
                work_execution
                and delivery is not None
                and (
                    not isinstance(delivery, dict)
                    or not isinstance(delivery.get("summary"), str)
                    or not delivery["summary"].strip()
                    or not isinstance(delivery.get("links"), list)
                    or not all(isinstance(link, str) for link in delivery["links"])
                )
            ):
                self._publish_reply(db, task, "这轮交付格式无效，成果尚未登记。", reply_to)
                self._work().finish(db, work_execution, "blocked")
                self.store.log(db, "scheduler", "invalid_work_delivery", {"task": task_id})
                return
            if reply:
                message_id = self._publish_reply(db, task, reply, reply_to)
                if attachment_ids:
                    files.bind(db, agent["id"], project["id"], message_id, attachment_ids)
                if private and audience == "group":
                    self._notify_delegator(db, task, original, reply_to, message_id)
            else:
                self.store.clear_draft(db, task_id)
                message_id = None
            if work_execution:
                if delivery and message_id is not None:
                    self._work().finish(
                        db,
                        work_execution,
                        "delivered",
                        {
                            "summary": delivery["summary"].strip()[:16000],
                            "links": delivery["links"][:20],
                            "message_id": message_id,
                        },
                    )
                else:
                    self._work().finish(db, work_execution, "waiting" if reply or answer.get("request") else "blocked")
                # A scoped work turn may reply and attach its result, but cannot
                # mutate unrelated conversation/task/execution state.
                return
            if not private and answer.get("memory_notes"):
                try:
                    self.memory.add_agent_notes(db, agent["id"], project["id"], task_id, answer["memory_notes"])
                except ValueError:
                    self.store.log(db, "scheduler", "rejected_memory_notes", {"task": task_id})
            for message in answer.get("messages", []):
                if not isinstance(message, dict):
                    continue
                try:
                    recipient = message.get("recipient")
                    if private:
                        # The delegator and the executing agent's own owner stay
                        # reachable; anything wider must be granted by the task.
                        allowed = {original.get("reply_to"), agent["id"], agent["owner_id"]}
                        if audience == "group":
                            allowed.add(None)
                        if recipient not in allowed:
                            raise ValueError("Private turn cannot broadcast or forward to other agents")
                        if recipient is None and group_posts >= 1:
                            raise ValueError("One group message per task")
                    self.store._message(
                        db,
                        project["id"],
                        agent["id"],
                        "agent",
                        message["body"],
                        recipient,
                        task_id,
                    )
                    if recipient is None and private:
                        group_posts += 1
                except (ValueError, KeyError, TypeError):
                    self.store.log(db, "scheduler", "rejected_message", {"task": task_id})
            if project["admin_agent_id"] == agent["id"]:
                for assignment in answer.get("tasks", []):
                    db.execute("SAVEPOINT delegation")
                    try:
                        target = db.execute(
                            "SELECT * FROM agents WHERE id=? AND project_id=? AND enabled=1",
                            (assignment["agent"], project["id"]),
                        ).fetchone()
                        if not target:
                            raise ValueError("Target not in project")
                        same_agent = target["id"] == agent["id"]
                        assignment["reply_to"] = original.get("reply_to") if same_agent else agent["id"]
                        assignment["private_turn"] = private if same_agent else True
                        # A task handed to another person's machine runs on their
                        # hardware and their CLI account; the owner is told that a
                        # task exists, not what it says.
                        borrowed = (not same_agent) and target["owner_id"] != agent["owner_id"]
                        notice = None
                        if not same_agent:
                            notice = self.store._message(
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
                            source_message_id=notice["id"] if notice else None,
                            parent_task_id=task_id,
                            wake_source_id=notice["id"] if notice else None,
                        )
                        db.execute("RELEASE delegation")
                        if borrowed:
                            # Only the borrowed agent can talk to its own owner:
                            # a human may read human_agent conversations of the
                            # agents they own, and nobody else's.
                            requester = db.execute(
                                "SELECT username FROM users WHERE id=?",
                                (agent["owner_id"],),
                            ).fetchone()
                            self.store._message(
                                db,
                                project["id"],
                                target["id"],
                                "agent",
                                "我接到一项任务："
                                + assignment["title"]
                                + ("（派活人：" + requester[0] + "）" if requester else "")
                                + "。任务细节只对派活人和我可见，结果会回到派活人。",
                                target["owner_id"],
                                task_id,
                            )
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
                    "SELECT e.*,a.node,a.backend_project FROM executions e JOIN agents a ON e.agent_id=a.id "
                    "WHERE e.state IN ('approved','starting','running','unknown') OR (e.state='awaiting_owner' AND ?)",
                    (isinstance(self.backend, PullBackend),),
                )
            ]
        for row in rows:
            if execution_id and row["id"] != execution_id:
                continue
            backend = self.backend.for_node(row["node"]) if hasattr(self.backend, "for_node") else self.backend
            awaiting_owner = row["state"] == "awaiting_owner"
            if awaiting_owner and not isinstance(backend, PullBackend):
                continue
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
                if awaiting_owner or row["state"] == "approved":
                    try:
                        result = backend.rpc(
                            {
                                "action": "execution_start",
                                "project": row["backend_project"],
                                "execution": row["id"],
                                "template": row["template"],
                            }
                        )
                    except (PermissionError, ValueError) as exc:
                        if not awaiting_owner:
                            raise
                        result = {"status": "failed", "error": str(exc)[:2000]}
                else:
                    result = backend.rpc(
                        {
                            "action": "execution_cancel" if row.get("cancel_requested") else "execution_status",
                            "execution": row["id"],
                        }
                    )
                state = result.get("status", "unknown")
                with self.store.connect() as db:
                    result_json = json.dumps(result, ensure_ascii=False)
                    if awaiting_owner:
                        if not db.execute(
                            "UPDATE executions SET state=?,result=? WHERE id=? AND state='awaiting_owner'",
                            (state, result_json, row["id"]),
                        ).rowcount:
                            continue
                    else:
                        db.execute("UPDATE executions SET state=?,result=? WHERE id=?", (state, result_json, row["id"]))
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
                            "后台作业 " + row["id"] + " 已结束：" + state + "。结果：" + result_json[-6000:],
                            row["agent_id"],
                            "execution-completed",
                        )
                        db.execute("UPDATE executions SET notified=1 WHERE id=?", (row["id"],))
            except Exception as exc:
                if awaiting_owner:
                    # The durable pull ID makes staging safe to retry after a failed enqueue or state update.
                    logging.exception("Could not stage execution for machine approval")
                    continue
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
        with self.store.connect() as db:
            self._work().schedule(db)
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
                        self._work().schedule(db)
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
                                "SELECT id FROM executions WHERE state IN ('approved','starting','running','unknown') "
                                "OR (state='awaiting_owner' AND ?)",
                                (isinstance(self.backend, PullBackend),),
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
