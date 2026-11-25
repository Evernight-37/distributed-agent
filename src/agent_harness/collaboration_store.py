"""Project groups, identities, ownership and recipient-filtered durable messages."""

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

from agent_harness.storage import migrate_database
from agent_harness.health import HealthCode, component, normalize_exception, report


MAX_EVENTS_PER_ACCOUNT = 512
MAX_EVENT_WAIT_SECONDS = 20
ACTIVE_TASK_STATES = {"queued", "starting", "running", "unknown"}
PERMISSION_REQUEST_PREFIX = "permission-request:"


def uid(prefix):
    return prefix + "-" + uuid.uuid4().hex


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "collaboration.sqlite3"
        self.migration = migrate_database(self.database, self.root / "backups")
        self.housekeep()

    def housekeep(self, now=None):
        """Remove only expired or unbound ephemeral records."""

        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("DELETE FROM invitations WHERE expires<?", (now,))
            db.execute("DELETE FROM account_challenges WHERE expires<?", (now,))
            db.execute("DELETE FROM account_requests WHERE expires<?", (now,))
            db.execute("DELETE FROM pull_pairs WHERE expires<?", (now,))
            db.execute(
                "DELETE FROM attachments WHERE message_id IS NULL AND created_at<?",
                (now - 86400,),
            )

    def _permission_command(self, user, project, verb, name):
        """Apply /allow or /deny and report it as a system message."""
        with self.connect() as db:
            self.require_member(db, project, user)
            agents = db.execute(
                "SELECT id FROM agents WHERE project_id=? AND name=?", (project, name.strip())
            ).fetchall()
            if not agents:
                raise ValueError("没有这个 Agent：" + name.strip())
            if len(agents) != 1:
                raise ValueError("有多个同名 Agent，请从项目成员列表选择")
        return self.agent_permission(user, project, agents[0]["id"], verb == "/allow")

    def agent_permission(self, user, project, agent_id, allow):
        """Compatibility entry point for an unambiguous pending request."""
        if type(allow) is not bool:
            raise ValueError("授权决定必须为布尔值")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require_member(db, project, user)
            requests = db.execute(
                "SELECT * FROM permission_requests WHERE project_id=? AND agent_id=? AND owner_id=? "
                "AND state='pending' LIMIT 2",
                (project, agent_id, user),
            ).fetchall()
            if len(requests) != 1:
                raise ValueError("请从 Agent 的私聊中选择一条待处理的提权申请")
            return self._decide_permission(db, user, requests[0], allow)

    def post_permission_request(self, db, project, agent, source_task, request):
        if (
            source_task["state"] not in {"needs_review", "failed"}
            or source_task["project_id"] != project
            or source_task["agent_id"] != agent["id"]
            or not isinstance(request, dict)
            or not isinstance(request.get("reason"), str)
            or not request["reason"].strip()
            or request.get("scope") not in (None, "full_access")
        ):
            return
        reason = request["reason"].strip()[:12000]
        owner = agent["owner_id"]
        if not db.execute("SELECT 1 FROM project_users WHERE project_id=? AND user_id=?", (project, owner)).fetchone():
            return
        contract = json.loads(source_task["contract"])
        if contract.get("private_turn") and contract.get("reply_to") != owner:
            reason = "一条私有任务申请在本机单次提权。"
        request_id = uid("permission")
        inserted = db.execute(
            "INSERT INTO permission_requests(id,source_task_id,project_id,agent_id,owner_id,node,reason,state,created_at) "
            "VALUES(?,?,?,?,?,?,?,'pending',?) ON CONFLICT(source_task_id) DO NOTHING",
            (request_id, source_task["id"], project, agent["id"], owner, agent["node"], reason, time.time()),
        )
        if not inserted.rowcount:
            return
        self._message(
            db,
            project,
            "scheduler",
            "system",
            f"「{agent['name']}」申请本次续跑所需的额外权限：{reason}",
            recipient=owner,
            causal_task=PERMISSION_REQUEST_PREFIX + request_id,
            conversation_id=self._pair_conversation("human_agent", owner, agent["id"]),
        )

    def permission_decide(self, user, project, request_id, approve, *, db=None):
        if type(approve) is not bool:
            raise ValueError("授权决定必须为布尔值")
        with self.connect() if db is None else nullcontext(db) as connection:
            if db is None:
                connection.execute("BEGIN IMMEDIATE")
            self.require_member(connection, project, user)
            request = connection.execute(
                "SELECT * FROM permission_requests WHERE id=? AND project_id=?", (request_id, project)
            ).fetchone()
            if not request or request["owner_id"] != user:
                raise PermissionError("只有执行机器的所有者可以处理此申请")
            return self._decide_permission(connection, user, request, approve)

    def _decide_permission(self, db, user, request, approve):
        state, continuation = request["state"], request["continuation_task_id"]
        if state == "pending":
            project = self.require_member(db, request["project_id"], user)
            agent = self.require_agent_owner(db, request["agent_id"], user)
            if (
                agent["project_id"] != project["id"]
                or agent["node"] != request["node"]
                or not db.execute("SELECT 1 FROM nodes WHERE name=? AND owner_id=?", (agent["node"], user)).fetchone()
            ):
                raise PermissionError("此 Agent 的执行机器已变更")
            if approve:
                if not agent["enabled"]:
                    raise ValueError("只能批准已启用的 Agent 续跑")
                source = db.execute(
                    "SELECT * FROM tasks WHERE id=? AND project_id=? AND agent_id=? AND processed=1",
                    (request["source_task_id"], project["id"], agent["id"]),
                ).fetchone()
                if not source or source["state"] not in {"needs_review", "failed"}:
                    raise ValueError("原任务不能续跑")
                contract = json.loads(source["contract"])
                work_execution = None
                if contract.get("project_work_execution_id"):
                    work_execution = db.execute(
                        "SELECT e.* FROM project_work_executions e JOIN project_work_items w ON w.id=e.work_id "
                        "WHERE e.task_id=? AND w.project_id=? AND w.revision=e.revision "
                        "AND w.status IN ('waiting','blocked','queued') "
                        "AND NOT EXISTS (SELECT 1 FROM project_work_executions newer WHERE newer.work_id=e.work_id "
                        "AND newer.rowid>e.rowid)",
                        (source["id"], project["id"]),
                    ).fetchone()
                    if not work_execution or work_execution["id"] != contract["project_work_execution_id"]:
                        raise ValueError("项目任务已变更；请让当前执行重新申请权限")
                    contract["project_work_execution_id"] = uid("work-execution")
                continuation = self._task(
                    db, project, agent, contract, user, cutoff=source["cutoff"], parent_task_id=source["id"]
                )["id"]
                if work_execution:
                    started = time.time()
                    db.execute(
                        "INSERT INTO project_work_executions(id,work_id,task_id,state,revision,input_cutoff,created_at) "
                        "VALUES(?,?,?,'running',?,?,?)",
                        (
                            contract["project_work_execution_id"],
                            work_execution["work_id"],
                            continuation,
                            work_execution["revision"],
                            work_execution["input_cutoff"],
                            started,
                        ),
                    )
                    db.execute(
                        "UPDATE project_work_items SET status='running',updated_at=? WHERE id=?",
                        (started, work_execution["work_id"]),
                    )
                state = "approved"
            else:
                state = "denied"
            db.execute(
                "UPDATE permission_requests SET state=?,continuation_task_id=?,decided_at=? WHERE id=?",
                (state, continuation, time.time(), request["id"]),
            )
            self.log(db, user, "permission_decide", {"request": request["id"], "state": state})
            self.emit_account_event(db, [user], "permission", project["id"])
        elif approve != (state in {"approved", "consumed"}):
            raise ValueError("此提权申请已经处理")
        return {"id": request["id"], "agent_id": request["agent_id"], "state": state, "task_id": continuation}

    def grants(self, agent):
        """Scopes a human has granted one agent, e.g. full_access."""
        from agent_harness.common import read_json

        value = read_json(self.root / "grants.json", {})
        entry = value.get(agent) if isinstance(value, dict) else None
        return sorted(entry) if isinstance(entry, list) else []

    def set_grant(self, agent, scope, allow, actor="owner", *, db=None):
        """Grant or revoke one scope; the file is small and written atomically."""
        from agent_harness.common import lock, read_json, write_json

        with self.connect() if db is None else nullcontext(db) as connection:
            if db is None:
                connection.execute("BEGIN IMMEDIATE")
            with lock(self.root / "grants.lock"):
                value = read_json(self.root / "grants.json", {})
                if not isinstance(value, dict):
                    value = {}
                current = set(value.get(agent) or []) if isinstance(value.get(agent), list) else set()
                if allow:
                    current.add(scope)
                else:
                    current.discard(scope)
                if current:
                    value[agent] = sorted(current)
                else:
                    value.pop(agent, None)
                write_json(self.root / "grants.json", value)
            self.log(connection, actor, "agent_grant", {"agent": agent, "scope": scope, "allow": bool(allow)})
        return sorted(current)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def log(self, db, actor, action, details):
        db.execute(
            "INSERT INTO audit(actor,action,details,created_at) VALUES(?,?,?,?)",
            (actor, action, json.dumps(details, ensure_ascii=False), time.time()),
        )

    def _emit_accounts(self, db, accounts, project, kind):
        """Advance private per-account wake cursors without storing user content."""

        if not isinstance(project, str) or not project:
            raise ValueError("事件必须属于项目")
        if not isinstance(kind, str) or not 1 <= len(kind) <= 40:
            raise ValueError("事件类别无效")
        now = time.time()
        for account in sorted(set(accounts)):
            if not isinstance(account, str) or not account:
                continue
            db.execute(
                "INSERT INTO account_event_sequences(account_id,cursor) VALUES(?,1) "
                "ON CONFLICT(account_id) DO UPDATE SET cursor=cursor+1",
                (account,),
            )
            cursor = db.execute(
                "SELECT cursor FROM account_event_sequences WHERE account_id=?",
                (account,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO account_events(account_id,cursor,project_id,kind,created_at) VALUES(?,?,?,?,?)",
                (account, cursor, project, kind, now),
            )
            db.execute(
                "DELETE FROM account_events WHERE account_id=? AND cursor<=?",
                (account, cursor - MAX_EVENTS_PER_ACCOUNT),
            )

    def emit_account_event(self, db, accounts, kind, project="account"):
        """Wake an account for changes that are not tied to one project."""
        return self._emit_accounts(db, accounts, project, kind)

    def emit_project_event(self, db, project, kind):
        accounts = [
            row[0]
            for row in db.execute(
                "SELECT user_id FROM project_users WHERE project_id=?",
                (project,),
            )
        ]
        self._emit_accounts(db, accounts, project, kind)

    def emit_conversation_event(self, db, project, conversation, kind="message"):
        """Wake exactly the humans allowed to observe one conversation."""

        if conversation == self.group_conversation(project) or (
            isinstance(conversation, str) and conversation.startswith("agent_agent:")
        ):
            return self.emit_project_event(db, project, kind)
        parts = conversation.split(":") if isinstance(conversation, str) else []
        if len(parts) != 3:
            return None
        channel, first, second = parts
        if channel == "human_agent":
            accounts = [first]
        elif channel == "human_human":
            accounts = [first, second]
        else:
            return None
        members = {
            row[0]
            for row in db.execute(
                "SELECT user_id FROM project_users WHERE project_id=? AND user_id IN (?,?)",
                (project, first, second),
            )
        }
        self._emit_accounts(db, (account for account in accounts if account in members), project, kind)

    def emit_task_event(self, db, task, kind="task"):
        accounts = [
            row[0]
            for row in db.execute(
                "SELECT user_id FROM project_users WHERE project_id=?",
                (task["project_id"],),
            )
            if self.task_visible_to_human(db, task, row[0])
        ]
        self._emit_accounts(db, accounts, task["project_id"], kind)

    def emit_execution_event(self, db, execution, kind="execution"):
        recipient = execution["reply_to"]
        if (
            recipient is None
            or db.execute(
                "SELECT 1 FROM agents WHERE id=? AND project_id=?",
                (recipient, execution["project_id"]),
            ).fetchone()
        ):
            return self.emit_project_event(db, execution["project_id"], kind)
        self._emit_accounts(db, [recipient], execution["project_id"], kind)

    def event_cursor(self, user):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user,)).fetchone():
                raise PermissionError("账户未加入此协作服务")
            row = db.execute(
                "SELECT cursor FROM account_event_sequences WHERE account_id=?",
                (user,),
            ).fetchone()
            return int(row[0]) if row else 0

    def events_wait(self, user, after=0, wait_seconds=0):
        if isinstance(after, bool) or not isinstance(after, int) or not 0 <= after <= 2**63 - 1:
            raise ValueError("事件游标无效")
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, (int, float))
            or not 0 <= wait_seconds <= MAX_EVENT_WAIT_SECONDS
        ):
            raise ValueError("事件等待时间无效")
        deadline = time.monotonic() + float(wait_seconds)
        while True:
            cursor = self.event_cursor(user)
            if cursor > after:
                return {"cursor": cursor, "changed": True}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"cursor": cursor, "changed": False}
            time.sleep(min(0.1, remaining))

    def host_owner(self):
        with self.connect() as db:
            row = db.execute("SELECT owner_account_id,host_node FROM service_host WHERE id=1").fetchone()
            return dict(row) if row else None

    def service_id(self):
        with self.connect() as db:
            return db.execute("SELECT service_id FROM service_metadata WHERE id=1").fetchone()["service_id"]

    def health_status(self):
        try:
            with self.connect() as db:
                result = db.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError("database integrity check failed")
            storage = component("storage", "healthy", HealthCode.OK, "协作数据库完整")
        except Exception as exc:
            storage = normalize_exception(
                "storage",
                exc,
                code=HealthCode.STORAGE_ERROR,
                message="协作数据库状态异常；请由托管者在本机检查",
            )
        return report(
            [
                component("hub", "healthy", HealthCode.OK, "协作服务可响应"),
                storage,
            ]
        )

    def claim_local_host(self, account_id, host_node=None):
        """Local filesystem authority only; never expose as an HTTP operation."""
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("需要已解锁的账户身份")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner_account_id,host_node FROM service_host WHERE id=1").fetchone()
            if row and row["owner_account_id"] != account_id:
                raise PermissionError("这台机器的协作服务已属于其他账户")
            if not row:
                db.execute(
                    "INSERT INTO service_host(id,owner_account_id,host_node) VALUES(1,?,?)",
                    (account_id, host_node),
                )
            elif host_node and not row["host_node"]:
                db.execute("UPDATE service_host SET host_node=? WHERE id=1", (host_node,))
            return {
                "owner_account_id": account_id,
                "host_node": host_node or (row["host_node"] if row else None),
            }

    @staticmethod
    def group_conversation(project):
        return "group:" + project

    @staticmethod
    def _pair_conversation(kind, first, second):
        if kind == "human_agent":
            return f"human_agent:{first}:{second}"
        return kind + ":" + ":".join(sorted((first, second)))

    def _conversation_for_message(self, db, row):
        project, sender, kind, recipient = (
            row[key] for key in ("project_id", "sender_id", "sender_type", "recipient_id")
        )
        if recipient is None:
            return self.group_conversation(project)
        if kind == "system":
            return None  # Directed system diagnostics are not a human conversation.
        agent = db.execute("SELECT id FROM agents WHERE id=? AND project_id=?", (recipient, project)).fetchone()
        human = db.execute(
            "SELECT user_id FROM project_users WHERE user_id=? AND project_id=?",
            (recipient, project),
        ).fetchone()
        if kind == "human" and agent:
            return self._pair_conversation("human_agent", sender, recipient)
        if kind == "human" and human:
            return self._pair_conversation("human_human", sender, recipient)
        if kind == "agent" and human:
            return self._pair_conversation("human_agent", recipient, sender)
        if kind == "agent" and agent:
            return self._pair_conversation("agent_agent", sender, recipient)
        return None

    def _conversation_access(self, db, project, user, conversation_id):
        self.require_member(db, project, user)
        if conversation_id == self.group_conversation(project):
            return "group", None
        parts = conversation_id.split(":") if isinstance(conversation_id, str) else []
        if len(parts) != 3:
            raise PermissionError("无权访问此会话")
        kind, first, second = parts
        if kind == "human_agent" and first == user:
            if db.execute(
                "SELECT 1 FROM agents WHERE id=? AND project_id=? AND owner_id=?",
                (second, project, user),
            ).fetchone():
                return kind, second
        elif kind == "human_human" and user in {first, second} and first < second:
            partner = second if first == user else first
            if db.execute(
                "SELECT 1 FROM project_users WHERE user_id=? AND project_id=?",
                (partner, project),
            ).fetchone():
                return kind, partner
        elif kind == "agent_agent" and first < second:
            found = db.execute(
                "SELECT COUNT(*) FROM agents WHERE project_id=? AND id IN (?,?)",
                (project, first, second),
            ).fetchone()[0]
            if found == 2:
                return kind, None
        raise PermissionError("无权访问此会话")

    def require_member(self, db, project, user):
        row = db.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
        if (
            not row
            or not db.execute(
                "SELECT 1 FROM project_users WHERE project_id=? AND user_id=?",
                (project, user),
            ).fetchone()
        ):
            raise PermissionError("无权访问这个项目")
        return dict(row)

    def require_agent_owner(self, db, agent, user):
        row = db.execute("SELECT * FROM agents WHERE id=?", (agent,)).fetchone()
        if not row or row["owner_id"] != user:
            raise PermissionError("只有所有者可以控制这个 agent")
        self.require_member(db, row["project_id"], user)
        return dict(row)

    def project_create(self, user, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError("项目名称不能为空，最多 120 字符")
        with self.connect() as db:
            host = db.execute("SELECT owner_account_id FROM service_host WHERE id=1").fetchone()
            if not host or host["owner_account_id"] != user:
                raise PermissionError("只有这台协作服务所属账户可以在此创建项目")
            project = uid("project")
            db.execute(
                "INSERT INTO projects(id,name,owner_id) VALUES(?,?,?)",
                (project, name.strip(), user),
            )
            db.execute("INSERT INTO project_users VALUES(?,?)", (project, user))
            self.emit_project_event(db, project, "project")
            self.log(db, user, "project_create", {"project": project})
            return {"id": project, "name": name.strip()}

    def invite(self, user, project):
        token = secrets.token_urlsafe(24)
        with self.connect() as db:
            row = self.require_member(db, project, user)
            if row["owner_id"] != user:
                raise PermissionError("只有群主可以邀请成员")
            db.execute(
                "INSERT INTO invitations VALUES(?,?,?,0)",
                (digest(token), project, time.time() + 86400),
            )
            self.log(db, user, "invite", {"project": project})
        return {"invite": token, "expires_in": 86400}

    def join(self, user, token):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            invitation = db.execute(
                "SELECT * FROM invitations WHERE hash=? AND used=0 AND expires>?",
                (digest(token), time.time()),
            ).fetchone()
            if not invitation:
                raise PermissionError("邀请无效或已使用")
            db.execute(
                "INSERT OR IGNORE INTO project_users VALUES(?,?)",
                (invitation["project_id"], user),
            )
            db.execute("UPDATE invitations SET used=1 WHERE hash=?", (digest(token),))
            self.emit_project_event(db, invitation["project_id"], "membership")
            return {"project": invitation["project_id"]}

    def add_agent(self, user, data, backend):
        from agent_harness.common import identifier

        identifier(data["backend_project"])
        identifier(data["node"])
        name = str(data["name"]).strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("agent 名称不能为空，最多 80 字符")
        with self.connect() as db:
            self.require_member(db, data["project"], user)
            node = db.execute("SELECT * FROM nodes WHERE name=?", (data["node"],)).fetchone()
            if not node or node["owner_id"] != user:
                raise PermissionError("只能接入自己的机器")
            if db.execute(
                "SELECT 1 FROM agents WHERE node=? AND backend_project=?",
                (data["node"], data["backend_project"]),
            ).fetchone():
                raise ValueError("执行会话已接入，不能在项目间共用私聊历史")
        # Existing registered projects are intentional user-selected worktrees; never guess paths.
        registered = backend.project(data["backend_project"])
        if registered.get("node") != data["node"]:
            raise ValueError("执行项目不属于所选节点")
        with self.connect() as db:
            agent = uid("agent")
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout,configuring) VALUES(?,?,?,?,?,?,?,?,?,1)",
                (
                    agent,
                    data["project"],
                    name,
                    user,
                    data["node"],
                    data["backend_project"],
                    data["model"],
                    data["effort"],
                    int(data["timeout"]),
                ),
            )
            self.emit_project_event(db, data["project"], "agent")
            self.log(db, user, "add_agent", {"agent": agent, "project": data["project"]})
        try:
            backend.configure(
                data["backend_project"],
                data["model"],
                data["effort"],
                int(data["timeout"]),
            )
        except Exception:
            # Preserve the mapping when the remote outcome is uncertain; never reuse its history.
            raise
        with self.connect() as db:
            db.execute("UPDATE agents SET configuring=0 WHERE id=?", (agent,))
            self.emit_project_event(db, data["project"], "agent")
        return {"id": agent}

    def set_admin(self, user, project, agent):
        with self.connect() as db:
            row = self.require_member(db, project, user)
            if row["owner_id"] != user:
                raise PermissionError("只有群主可以指定管理员")
            previous = db.execute(
                "SELECT admin_agent_id FROM projects WHERE id=?",
                (project,),
            ).fetchone()[0]
            if agent:
                if not db.execute("SELECT 1 FROM agents WHERE id=? AND project_id=?", (agent, project)).fetchone():
                    raise ValueError("agent 不在本项目")
            # An empty choice means "no coordinator": group posts then wake nobody
            # until a human mentions an agent. It must not be an error, because the
            # picker offers that state for projects that never had an admin.
            db.execute("UPDATE projects SET admin_agent_id=? WHERE id=?", (agent or None, project))
            self.emit_project_event(db, project, "project")
            self.log(db, user, "set_admin", {"project": project, "agent": agent or None})
            self._admin_notice(db, project, user, previous, agent or None)

    def _admin_notice(self, db, project, user, previous, current):
        """Tell the group about a handover the way a group chat announces it."""
        if previous == current:
            return
        who = db.execute("SELECT username FROM users WHERE id=?", (user,)).fetchone()
        who = who[0] if who else "群主"

        def label(value):
            row = db.execute(
                "SELECT name FROM agents WHERE id=? AND project_id=?",
                (value, project),
            ).fetchone()
            return row[0] if row else value

        if not current:
            body = "「" + who + "」取消了管理员设置，群里的人类发言不再自动唤醒 agent。"
        elif not previous:
            body = "「" + who + "」已将「" + label(current) + "」设为管理员。"
        else:
            body = (
                "「" + who + "」已将「" + label(current) + "」设为管理员；「" + label(previous) + "」现在是普通成员。"
            )
        self._message(db, project, "system", "system", body)

    def configure_agent(self, user, data, backend):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = self.require_agent_owner(db, data["agent"], user)
            if db.execute(
                "SELECT 1 FROM tasks WHERE agent_id=? AND state IN ('queued','starting','running','unknown')",
                (agent["id"],),
            ).fetchone():
                raise ValueError("请先等 agent 本轮结束；可暂停项目阻止新轮次")
            db.execute("UPDATE agents SET configuring=1 WHERE id=?", (agent["id"],))
            self.emit_project_event(db, agent["project_id"], "agent")
        backend.configure(
            agent["backend_project"],
            data["model"],
            data["effort"],
            int(data["timeout"]),
        )
        with self.connect() as db:
            db.execute(
                "UPDATE agents SET model=?,effort=?,timeout=?,enabled=?,configuring=0 WHERE id=?",
                (
                    data["model"],
                    data["effort"],
                    int(data["timeout"]),
                    int(bool(data["enabled"])),
                    agent["id"],
                ),
            )
            self.emit_project_event(db, agent["project_id"], "agent")
            self.log(
                db,
                user,
                "configure_agent",
                {
                    "agent": agent["id"],
                    "model": data["model"],
                    "effort": data["effort"],
                },
            )
        return {"updated": agent["id"]}

    def message(
        self,
        user,
        project,
        body,
        recipient=None,
        mention_agent_id=None,
        conversation_id=None,
        client_message_id=None,
        sender_client=None,
        attachment_ids=None,
    ):
        from agent_harness.attachments import Attachments

        # A human can answer a permission request from the chat: "/allow 名字" or
        # "/deny 名字". This is the one place a grant changes, so the boundary the
        # agent cannot cross is decided by a person.
        words = str(body or "").strip().split()
        if words and words[0] in ("/allow", "/deny") and len(words) >= 2:
            return self._permission_command(user, project, words[0], " ".join(words[1:]))

        attachments = Attachments(self)
        attachment_ids = attachments.ids(attachment_ids if attachment_ids is not None else [])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require_member(db, project, user)
            if conversation_id is not None:
                kind, partner = self._conversation_access(db, project, user, conversation_id)
                if kind == "agent_agent":
                    raise PermissionError("人类不能以 Agent 身份发送内部私聊")
                if recipient is not None and recipient != partner:
                    raise ValueError("会话与收件人不一致")
                recipient = partner
            if recipient == user:
                raise ValueError("不能给自己发送私聊")
            if recipient:
                agent = db.execute(
                    "SELECT owner_id FROM agents WHERE id=? AND project_id=?",
                    (recipient, project),
                ).fetchone()
                if agent and agent["owner_id"] != user:
                    raise PermissionError("只能私聊你自己的 Agent")
            if client_message_id is not None:
                try:
                    parsed = uuid.UUID(client_message_id)
                except (ValueError, TypeError, AttributeError):
                    raise ValueError("消息幂等标识必须为 UUID") from None
                if str(parsed) != client_message_id.lower():
                    raise ValueError("消息幂等标识必须为规范 UUID")
                existing = db.execute(
                    "SELECT * FROM messages WHERE project_id=? AND sender_id=? AND client_message_id=?",
                    (project, user, client_message_id.lower()),
                ).fetchone()
                if existing:
                    expected_body = body.strip() if isinstance(body, str) else body
                    if (
                        existing["sender_type"] != "human"
                        or existing["body"] != expected_body
                        or existing["recipient_id"] != recipient
                        or existing["mention_agent_id"] != mention_agent_id
                        or existing["sender_client"] != sender_client
                        or sorted(a["id"] for a in attachments.metadata(db, existing["id"])) != sorted(attachment_ids)
                    ):
                        raise ValueError("消息幂等标识已用于其他内容")
                    return {
                        "id": existing["id"],
                        "conversation_id": existing["conversation_id"],
                        "duplicate": True,
                    }
                client_message_id = client_message_id.lower()
            result = self._message(
                db,
                project,
                user,
                "human",
                body,
                recipient,
                mention_agent_id=mention_agent_id,
                client_message_id=client_message_id,
                sender_client=sender_client,
            )
            attachments.bind(db, user, project, result["id"], attachment_ids)
            return result

    def _message(
        self,
        db,
        project,
        sender,
        kind,
        body,
        recipient=None,
        causal_task=None,
        mention_agent_id=None,
        client_message_id=None,
        sender_client=None,
        *,
        conversation_id=None,
    ):
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 16000:
            raise ValueError("消息不能为空，最多 16000 字符")
        if recipient and not (
            db.execute("SELECT 1 FROM agents WHERE id=? AND project_id=?", (recipient, project)).fetchone()
            or db.execute(
                "SELECT 1 FROM project_users WHERE user_id=? AND project_id=?",
                (recipient, project),
            ).fetchone()
        ):
            raise ValueError("收件人必须在本项目")
        if mention_agent_id:
            if recipient or kind != "human":
                raise ValueError("只能在公开的人类消息中呼叫 Agent")
            if not isinstance(mention_agent_id, str):
                raise ValueError("Agent 标识无效")
            if not db.execute(
                "SELECT 1 FROM agents WHERE id=? AND project_id=? AND enabled=1 AND configuring=0",
                (mention_agent_id, project),
            ).fetchone():
                raise ValueError("要呼叫的 Agent 不在项目中或尚未启用")
        conversation = conversation_id or self._conversation_for_message(
            db,
            {
                "project_id": project,
                "sender_id": sender,
                "sender_type": kind,
                "recipient_id": recipient,
            },
        )
        cursor = db.execute(
            "INSERT INTO messages(project_id,sender_id,sender_type,recipient_id,body,created_at,causal_task,mention_agent_id,conversation_id,client_message_id,sender_client) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                project,
                sender,
                kind,
                recipient,
                body.strip(),
                time.time(),
                causal_task,
                mention_agent_id,
                conversation,
                client_message_id,
                sender_client,
            ),
        )
        self.emit_conversation_event(db, project, conversation)
        return {"id": cursor.lastrowid, "conversation_id": conversation}

    def draft_message(self, db, project, sender, body, recipient, task_id):
        if not isinstance(body, str) or len(body) > 16000:
            return None
        key = "draft:" + task_id
        row = db.execute("SELECT id FROM messages WHERE causal_task=?", (key,)).fetchone()
        if not body.strip():
            return row["id"] if row else None
        if row:
            conversation = self._conversation_for_message(
                db,
                {
                    "project_id": project,
                    "sender_id": sender,
                    "sender_type": "agent",
                    "recipient_id": recipient,
                },
            )
            db.execute(
                "UPDATE messages SET body=?,recipient_id=?,conversation_id=? WHERE id=?",
                (body.strip(), recipient, conversation, row["id"]),
            )
            self.emit_conversation_event(db, project, conversation, "draft")
            return row["id"]
        return self._message(db, project, sender, "agent", body, recipient, key)["id"]

    def clear_draft(self, db, task_id):
        row = db.execute(
            "SELECT project_id,conversation_id FROM messages WHERE causal_task=?",
            ("draft:" + task_id,),
        ).fetchone()
        db.execute("DELETE FROM messages WHERE causal_task=?", ("draft:" + task_id,))
        if row:
            self.emit_conversation_event(db, row["project_id"], row["conversation_id"], "draft")

    def finalize_draft(self, db, task_id, body, recipient):
        row = db.execute("SELECT id FROM messages WHERE causal_task=?", ("draft:" + task_id,)).fetchone()
        if not row:
            return False
        if not body:
            message = db.execute("SELECT project_id,conversation_id FROM messages WHERE id=?", (row["id"],)).fetchone()
            db.execute("DELETE FROM messages WHERE id=?", (row["id"],))
            if message:
                self.emit_conversation_event(db, message["project_id"], message["conversation_id"], "draft")
        else:
            message = db.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()
            conversation = self._conversation_for_message(
                db,
                {
                    "project_id": message["project_id"],
                    "sender_id": message["sender_id"],
                    "sender_type": "agent",
                    "recipient_id": recipient,
                },
            )
            db.execute(
                "UPDATE messages SET body=?,recipient_id=?,conversation_id=?,causal_task=? WHERE id=?",
                (body, recipient, conversation, task_id, row["id"]),
            )
            self.emit_conversation_event(db, message["project_id"], conversation, "message")
        return True

    def visible_messages(self, agent, after=0, cutoff=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent,)).fetchone()
            if not row:
                raise ValueError("未知 agent")
            return [
                dict(x)
                for x in db.execute(
                    "SELECT * FROM messages WHERE project_id=? AND id>? AND id<=? AND (causal_task IS NULL OR causal_task NOT LIKE 'draft:%') AND (recipient_id IS NULL OR recipient_id=? OR sender_id=?) AND NOT (sender_type='human' AND recipient_id IS NOT NULL AND recipient_id=? AND sender_id<>?) ORDER BY id",
                    (
                        row["project_id"],
                        after,
                        cutoff if cutoff is not None else 9223372036854775807,
                        agent,
                        agent,
                        agent,
                        row["owner_id"],
                    ),
                ).fetchall()
            ]

    def _task_source(self, db, project, source_message_id):
        if type(source_message_id) is not int or not 0 < source_message_id < 2**63:
            raise ValueError("原消息编号无效")
        source = db.execute(
            "SELECT * FROM messages WHERE id=? AND project_id=? AND (causal_task IS NULL OR causal_task NOT LIKE 'draft:%')",
            (source_message_id, project),
        ).fetchone()
        if not source:
            raise PermissionError("无权引用此消息")
        return source

    @staticmethod
    def task_reply_recipient(agent, contract):
        if contract.get("private_turn"):
            if contract.get("audience") == "group":
                return None
            if contract.get("audience") == "owner":
                return agent["owner_id"]
        return contract.get("reply_to")

    def task(self, user, data, delegate=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            project = self.require_member(db, data["project"], user)
            agent = self.require_agent_owner(db, data["agent"], user)
            if agent["project_id"] != data["project"] or not agent["enabled"]:
                raise ValueError("请选择可用的项目 agent")
            audience = data.get("audience")
            if audience not in (None, "group", "private", "owner"):
                raise ValueError("audience 只能是 group、private 或 owner")
            contract = dict(data)
            if audience in {"private", "owner"}:
                # The report goes to this person's private conversation with the
                # agent instead of the project group; the identity stays the same.
                # "owner" and "private" coincide here because the caller must own
                # the agent, but both spellings reach this method.
                contract.update(private_turn=True, reply_to=user)
            source_message_id = contract.pop("source_message_id", None)
            if source_message_id is not None:
                source = self._task_source(db, project["id"], source_message_id)
                self._conversation_access(db, project["id"], user, source["conversation_id"])
            return self._task(db, project, agent, contract, user, delegate, source_message_id=source_message_id)

    def _task(
        self,
        db,
        project,
        agent,
        data,
        actor,
        delegate=None,
        *,
        cutoff=None,
        source_message_id=None,
        parent_task_id=None,
        wake_source_id=None,
    ):
        if agent.get("configuring"):
            raise ValueError("机器配置尚未确认；请所有者重新确认设置")
        if project["paused"]:
            raise ValueError("项目已暂停")
        stage = data.get("stage", "discuss")
        if stage not in {"discuss", "review", "implement"}:
            raise ValueError("未知任务阶段")
        if stage == "implement" and not data.get("allowed_files", "").strip():
            raise ValueError("实现任务需要明确文件所有权")
        for key in ("title", "instructions", "acceptance", "validation"):
            if not isinstance(data.get(key), str) or not data[key].strip() or len(data[key]) > 16000:
                raise ValueError("任务目标、验收与验证不能为空且不得过长")
        if db.execute(
            "SELECT 1 FROM tasks WHERE agent_id=? AND state IN ('queued','starting','running','unknown')",
            (agent["id"],),
        ).fetchone():
            raise ValueError("这个 agent 已有活动任务")
        if parent_task_id is not None:
            parent = db.execute(
                "SELECT r.source_message_id FROM tasks t LEFT JOIN task_trace r ON r.task_id=t.id "
                "WHERE t.id=? AND t.project_id=?",
                (parent_task_id, project["id"]),
            ).fetchone()
            if parent is None:
                raise ValueError("上级任务不属于当前项目")
            if source_message_id is None:
                source_message_id = parent["source_message_id"]
        for message_id in {source_message_id, wake_source_id} - {None}:
            self._task_source(db, project["id"], message_id)
        task = uid("task")
        if cutoff is None:
            cutoff = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM messages WHERE project_id=?",
                (project["id"],),
            ).fetchone()[0]
        db.execute(
            "INSERT INTO tasks(id,project_id,agent_id,title,contract,state,created_at,cutoff) VALUES(?,?,?,?,?,?,?,?)",
            (
                task,
                project["id"],
                agent["id"],
                data["title"],
                json.dumps(data, ensure_ascii=False),
                "queued",
                time.time(),
                cutoff,
            ),
        )
        private = bool(data.get("private_turn"))
        recipient = data.get("reply_to") if private else None
        conversation = self._conversation_for_message(
            db,
            {
                "project_id": project["id"],
                "sender_id": agent["id"],
                "sender_type": "agent",
                "recipient_id": recipient,
            },
        )
        delivery = self._conversation_for_message(
            db,
            {
                "project_id": project["id"],
                "sender_id": agent["id"],
                "sender_type": "agent",
                "recipient_id": self.task_reply_recipient(agent, data),
            },
        )
        from agent_harness.owner_backend import device_node

        node_name = next(
            (
                row["name"]
                for row in db.execute(
                    "SELECT public_key,name FROM account_devices WHERE account_id=?", (agent["owner_id"],)
                )
                if device_node(row["public_key"]) == agent["node"]
            ),
            None,
        )
        db.execute(
            "INSERT INTO task_trace(task_id,source_message_id,parent_task_id,wake_source_id,node,node_name,"
            "delivery_conversation_id) VALUES(?,?,?,?,?,?,?)",
            (task, source_message_id, parent_task_id, wake_source_id, agent["node"], node_name, delivery),
        )
        db.execute(
            "INSERT INTO messages(project_id,sender_id,sender_type,recipient_id,body,created_at,causal_task,conversation_id) VALUES(?,?,?,?,?,?,?,?)",
            (
                project["id"],
                agent["id"],
                "agent",
                recipient,
                "（正在回复…）",
                time.time(),
                "draft:" + task,
                conversation,
            ),
        )
        self.emit_conversation_event(db, project["id"], conversation, "task")
        origin = {"task": task, "agent": agent["id"]}
        if delegate is not None:
            origin["delegate"] = digest(delegate)
        self.log(db, actor, "task", origin)
        return {"id": task, "state": "queued"}

    def _may_cancel_task(self, db, task, user, delegate=None):
        if (
            delegate is None
            and db.execute("SELECT 1 FROM agents WHERE id=? AND owner_id=?", (task["agent_id"], user)).fetchone()
        ):
            return True
        origin = db.execute(
            "SELECT actor,json_extract(details,'$.delegate') AS delegate FROM audit "
            "WHERE action='task' AND json_extract(details,'$.task')=? ORDER BY id LIMIT 1",
            (task["id"],),
        ).fetchone()
        return bool(origin and origin["actor"] == user and (delegate is None or origin["delegate"] == digest(delegate)))

    def cancel_task(self, user, project, task_id, delegate=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require_member(db, project, user)
            task = db.execute("SELECT * FROM tasks WHERE id=? AND project_id=?", (task_id, project)).fetchone()
            if (
                not task
                or not self.task_visible_to_human(db, task, user)
                or not self._may_cancel_task(db, task, user, delegate)
            ):
                raise PermissionError("无法取消此任务")
            state = task["state"]
            pending = bool(task["cancel_requested"])
            if state in ACTIVE_TASK_STATES and not pending:
                if state == "queued" and not task["job"]:
                    state = "cancelled"
                    db.execute("UPDATE tasks SET state=?,processed=0 WHERE id=?", (state, task_id))
                else:
                    pending = True
                    db.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (task_id,))
                self.log(db, user, "task_cancel", {"task": task_id})
                self.emit_task_event(db, task, "task")
            return {"id": task_id, "state": state, "cancel_requested": pending and state in ACTIVE_TASK_STATES}

    def snapshot(self, user):
        from agent_harness.common import read_json

        with self.connect() as db:
            projects = [
                dict(x)
                for x in db.execute(
                    "SELECT p.* FROM projects p JOIN project_users m ON p.id=m.project_id WHERE m.user_id=?",
                    (user,),
                )
            ]
            nodes = []
            for n in db.execute("SELECT * FROM nodes WHERE owner_id=?", (user,)):
                config = read_json(self.root / "nodes" / (n["name"] + ".json"), {})
                nodes.append(
                    {
                        **dict(n),
                        **{k: v for k, v in config.items() if k in {"host", "platform"}},
                    }
                )
            # People cannot enumerate unrelated users/projects.
            people = [
                dict(x)
                for x in db.execute(
                    "SELECT DISTINCT u.id,u.username FROM users u JOIN project_users a ON u.id=a.user_id JOIN project_users b ON a.project_id=b.project_id WHERE b.user_id=?",
                    (user,),
                )
            ]
            return {"projects": projects, "nodes": nodes, "users": people}

    def mark_conversation_read(self, user, project, conversation_id):
        with self.connect() as db:
            self._conversation_access(db, project, user, conversation_id)
            latest = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM messages WHERE project_id=? AND conversation_id=?",
                (project, conversation_id),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO conversation_reads(project_id,user_id,conversation_id,last_message) VALUES(?,?,?,?) ON CONFLICT(project_id,user_id,conversation_id) DO UPDATE SET last_message=MAX(last_message,excluded.last_message)",
                (project, user, conversation_id, latest),
            )
            self._emit_accounts(db, [user], project, "read")
            return {"conversation_id": conversation_id, "last_message": latest}

    def _human_visible_messages(self, db, project, user):
        group = self.group_conversation(project)
        # Query visibility before LIMIT, otherwise busy private conversations can
        # push this user's messages out of the window.
        return db.execute(
            """SELECT * FROM messages WHERE project_id=? AND
            (conversation_id=? OR conversation_id LIKE ? OR conversation_id LIKE ?
             OR conversation_id LIKE ? OR conversation_id LIKE 'agent_agent:%')
            ORDER BY id DESC LIMIT 500""",
            (
                project,
                group,
                "human_agent:" + user + ":%",
                "human_human:" + user + ":%",
                "human_human:%:" + user,
            ),
        ).fetchall()[::-1]

    def _conversation_list(self, db, project, user, messages, members, people):
        names = {r["id"]: r["name"] for r in members}
        names.update({r["id"]: r["username"] for r in people})
        found = {self.group_conversation(project)}
        found.update(m["conversation_id"] for m in messages if m["conversation_id"])
        # Include existing direct conversations even when their latest message
        # fell outside the 500-message window.
        for row in db.execute(
            """SELECT DISTINCT conversation_id FROM messages WHERE project_id=? AND
            (conversation_id LIKE ? OR conversation_id LIKE ? OR conversation_id LIKE ? OR conversation_id LIKE 'agent_agent:%')""",
            (
                project,
                "human_agent:" + user + ":%",
                "human_human:" + user + ":%",
                "human_human:%:" + user,
            ),
        ):
            if row["conversation_id"]:
                found.add(row["conversation_id"])
        reads = {
            r["conversation_id"]: r["last_message"]
            for r in db.execute(
                "SELECT conversation_id,last_message FROM conversation_reads WHERE project_id=? AND user_id=?",
                (project, user),
            )
        }
        result = []
        for cid in found:
            try:
                kind, partner = self._conversation_access(db, project, user, cid)
            except PermissionError:
                continue
            if kind == "group":
                title = "项目大群"
                participants = [r["id"] for r in people] + [r["id"] for r in members]
            elif kind == "agent_agent":
                participants = cid.split(":")[1:]
                title = " ↔ ".join(names.get(pid, "Agent") for pid in participants)
            else:
                participants = [user, partner]
                title = names.get(partner, "成员")
            last = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM messages WHERE project_id=? AND conversation_id=?",
                (project, cid),
            ).fetchone()[0]
            unread = db.execute(
                """SELECT COUNT(*) FROM messages WHERE project_id=? AND conversation_id=? AND id>?
                AND sender_id<>? AND (causal_task IS NULL OR causal_task NOT LIKE 'draft:%')""",
                (project, cid, reads.get(cid, 0), user),
            ).fetchone()[0]
            result.append(
                {
                    "id": cid,
                    "kind": kind,
                    "title": title,
                    "unread_count": unread,
                    "participants": participants,
                    "last_message": last,
                }
            )
        return sorted(result, key=lambda c: (c["kind"] != "group", -c["last_message"], c["title"]))

    def task_visible_to_human(self, db, task, user):
        try:
            contract = json.loads(task["contract"] or "{}")
        except (ValueError, TypeError):
            return False
        if not contract.get("private_turn"):
            return True
        recipient = contract.get("reply_to")
        if recipient == user:
            return True
        return bool(
            db.execute(
                "SELECT 1 FROM agents WHERE id=? AND project_id=?",
                (recipient, task["project_id"]),
            ).fetchone()
        )

    def task_trace_for_human(self, db, task, user):
        """Return only work links visible to the same project member as the task."""
        project = task["project_id"]
        agent = db.execute(
            "SELECT name FROM agents WHERE id=? AND project_id=?", (task["agent_id"], project)
        ).fetchone()
        trace = {
            "agent_id": task["agent_id"],
            "agent_name": agent["name"] if agent else None,
            "node": None,
            "node_name": None,
            "source_message_id": None,
            "parent_task_id": None,
            "delivery_conversation_id": None,
        }
        row = db.execute("SELECT * FROM task_trace WHERE task_id=?", (task["id"],)).fetchone()
        if row is None:
            return trace
        trace.update(node=row["node"], node_name=row["node_name"])
        if row["source_message_id"] is not None:
            try:
                source = self._task_source(db, project, row["source_message_id"])
                self._conversation_access(db, project, user, source["conversation_id"])
            except PermissionError:
                pass
            else:
                trace["source_message_id"] = source["id"]
        if row["parent_task_id"]:
            parent = db.execute(
                "SELECT * FROM tasks WHERE id=? AND project_id=?", (row["parent_task_id"], project)
            ).fetchone()
            if parent and self.task_visible_to_human(db, parent, user):
                trace["parent_task_id"] = parent["id"]
        if row["delivery_conversation_id"]:
            try:
                self._conversation_access(db, project, user, row["delivery_conversation_id"])
            except PermissionError:
                pass
            else:
                trace["delivery_conversation_id"] = row["delivery_conversation_id"]
        return trace

    def local_task_recovery(self, db, task, user, delegate=None):
        """Identify an unknown pull job that its own machine owner can inspect."""
        if delegate is not None or task["state"] != "unknown" or not task["job"]:
            return None
        row = db.execute(
            "SELECT j.id AS job_id,j.node FROM pull_jobs j JOIN agents a ON a.id=j.agent_id "
            "WHERE j.id=? AND j.kind='task' AND j.state IN ('running','unknown') "
            "AND a.id=? AND a.project_id=? AND a.owner_id=? AND a.node=j.node",
            (task["job"], task["agent_id"], task["project_id"], user),
        ).fetchone()
        return dict(row) if row else None

    def project_view(self, user, project, delegate=None):
        with self.connect() as db:
            p = self.require_member(db, project, user)
            members = []
            for row in db.execute("SELECT * FROM agents WHERE project_id=?", (project,)):
                item = dict(row)
                busy = db.execute(
                    "SELECT state FROM tasks WHERE agent_id=? AND state IN ('queued','starting','running','unknown')",
                    (row["id"],),
                ).fetchone()
                item.update(
                    role="admin" if p["admin_agent_id"] == row["id"] else "peer",
                    status="configuration_pending"
                    if row["configuring"]
                    else busy["state"]
                    if busy
                    else ("idle" if row["enabled"] else "disabled"),
                )
                members.append(item)
            permission_requests = (
                {
                    r["id"]: {
                        "id": r["id"],
                        "agent_id": r["agent_id"],
                        "state": r["state"],
                        "reason": r["reason"],
                        "task_id": r["continuation_task_id"],
                    }
                    for r in db.execute(
                        "SELECT * FROM permission_requests WHERE project_id=? AND owner_id=?", (project, user)
                    )
                }
                if delegate is None
                else {}
            )
            people = [
                dict(x)
                for x in db.execute(
                    "SELECT u.id,u.username FROM users u JOIN project_users p ON u.id=p.user_id WHERE p.project_id=?",
                    (project,),
                )
            ]
            messages = []
            trace_cache = {}
            for row in self._human_visible_messages(db, project, user):
                item = dict(row)
                try:
                    self._conversation_access(db, project, user, item["conversation_id"])
                except PermissionError:
                    continue
                table = "users" if item["sender_type"] == "human" else "agents"
                key = "username" if table == "users" else "name"
                sender = (
                    db.execute(f"SELECT {key} FROM {table} WHERE id=?", (item["sender_id"],)).fetchone()
                    if item["sender_type"] != "system"
                    else None
                )
                recipient = db.execute("SELECT name FROM agents WHERE id=?", (item["recipient_id"],)).fetchone()
                if not recipient:
                    recipient = db.execute("SELECT username FROM users WHERE id=?", (item["recipient_id"],)).fetchone()
                mention = (
                    db.execute(
                        "SELECT name FROM agents WHERE id=?",
                        (item["mention_agent_id"],),
                    ).fetchone()
                    if item["mention_agent_id"]
                    else None
                )
                item.update(
                    sender_name=sender[0] if sender else "系统",
                    recipient_name=recipient[0] if recipient else None,
                    mention_agent_name=mention[0] if mention else None,
                    channel="direct" if item["recipient_id"] else "group",
                )
                from agent_harness.attachments import Attachments

                item["attachments"] = Attachments(self).metadata(db, item["id"])
                if item["sender_type"] == "system" and (item["causal_task"] or "").startswith(
                    PERMISSION_REQUEST_PREFIX
                ):
                    request_id = item["causal_task"][len(PERMISSION_REQUEST_PREFIX) :]
                    if request_id in permission_requests:
                        item["permission_request"] = permission_requests[request_id]
                task_id = (item["causal_task"] or "").removeprefix("draft:")
                if task_id.startswith("task-"):
                    if task_id not in trace_cache:
                        task = db.execute(
                            "SELECT * FROM tasks WHERE id=? AND project_id=?", (task_id, project)
                        ).fetchone()
                        trace_cache[task_id] = (
                            self.task_trace_for_human(db, task, user)
                            if task and self.task_visible_to_human(db, task, user)
                            else None
                        )
                    trace = trace_cache[task_id]
                    if trace is None:
                        item["causal_task"] = None
                    else:
                        item["task_id"] = task_id
                        if trace["source_message_id"] is not None:
                            item["reply_to_message_id"] = trace["source_message_id"]
                messages.append(item)
            conversations = self._conversation_list(db, project, user, messages, members, people)
            tasks = []
            for row in db.execute(
                "SELECT * FROM tasks WHERE project_id=? ORDER BY created_at DESC LIMIT 100",
                (project,),
            ):
                item = dict(row)
                if not self.task_visible_to_human(db, item, user):
                    continue
                item["trace"] = trace_cache.get(item["id"]) or self.task_trace_for_human(db, item, user)
                item["can_cancel"] = (
                    item["state"] in ACTIVE_TASK_STATES
                    and not item["cancel_requested"]
                    and self._may_cancel_task(db, item, user, delegate)
                )
                if item["state"] == "unknown":
                    item["display_status"] = "待核对"
                    recovery = self.local_task_recovery(db, item, user, delegate)
                    if recovery:
                        item["local_recovery"] = recovery
                tasks.append(item)
            for task in tasks:
                if task["state"] == "needs_review" and task["processed"]:
                    try:
                        reply = json.loads(task["report"] or "{}").get("reply")
                    except (ValueError, AttributeError):
                        reply = None
                    task["display_status"] = (
                        "已处理 · 未发言" if isinstance(reply, str) and not reply.strip() else "已回复"
                    )
            executions = [
                dict(x)
                for x in db.execute(
                    """SELECT * FROM executions WHERE project_id=? AND
                (reply_to IS NULL OR reply_to=? OR reply_to IN (SELECT id FROM agents WHERE project_id=?))
                ORDER BY created_at DESC LIMIT 100""",
                    (project, user, project),
                )
            ]
            p["owner_name"] = next(
                (x["username"] for x in people if x["id"] == p["owner_id"]),
                p["owner_id"],
            )
            p["admin_agent_name"] = next((x["name"] for x in members if x["id"] == p["admin_agent_id"]), None)
            return {
                "project": p,
                "members": members,
                "messages": messages,
                "conversations": conversations,
                "tasks": tasks,
                "executions": executions,
                "users": people,
            }
