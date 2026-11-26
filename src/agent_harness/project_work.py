"""Revisioned project work proposals and durable work items."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from agent_harness.collaboration_store import uid
from agent_harness.workspace_ownership import DIRTY_WORKTREE


WORK_STATUSES = {"queued", "running", "waiting", "delivered", "accepted", "blocked"}
FIELDS = ("title", "instructions", "deliverable", "acceptance", "validation")


class ProjectWork:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _request_id(value):
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("工作请求幂等标识必须为 UUID") from None
        if str(parsed) != value.lower():
            raise ValueError("工作请求幂等标识必须为规范 UUID")
        return value.lower()

    @staticmethod
    def _hash(payload):
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _message(self, db, project, message_id):
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("引用消息编号无效")
        row = db.execute(
            "SELECT * FROM messages WHERE id=? AND project_id=? AND "
            "(causal_task IS NULL OR causal_task NOT LIKE 'draft:%')",
            (message_id, project),
        ).fetchone()
        if row is None:
            raise PermissionError("无权引用此消息")
        return row

    def _publish_card(self, db, row, kind, event, revision=None, message_id=None):
        """Publish a scoped, payload-free work card and persist its projection."""

        if message_id is None:
            sent = self.store._message(
                db,
                row["project_id"],
                "system",
                "system",
                "项目工作已更新",
                row["creator_id"] if row["visibility"] == "private" else None,
                conversation_id=row["conversation_id"],
            )
            message_id = sent["id"]
        db.execute(
            "INSERT OR IGNORE INTO project_work_cards(message_id,project_id,conversation_id,kind,record_id,"
            "event,revision,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                message_id,
                row["project_id"],
                row["conversation_id"],
                kind,
                row["id"],
                event,
                revision,
                time.time(),
            ),
        )
        return message_id

    def add_work_execution_card(self, db, execution_id, message_id, event="waiting"):
        """Attach a validated execution publication to its visible work card."""

        if event not in {"waiting", "delivered"}:
            raise ValueError("执行卡事件无效")
        row = db.execute(
            "SELECT w.*,e.task_id FROM project_work_executions e "
            "JOIN project_work_items w ON w.id=e.work_id WHERE e.id=?",
            (execution_id,),
        ).fetchone()
        if row is None:
            raise ValueError("工作执行不存在")
        message = self._message(db, row["project_id"], message_id)
        if message["conversation_id"] != row["conversation_id"] or message["causal_task"] != row["task_id"]:
            raise PermissionError("执行卡消息必须属于本次执行与约定会话")
        self._publish_card(db, row, "work", event, message_id=message_id)
        return message_id

    def project_cards(self, db, user, project, messages):
        """Return visible card tags keyed by the requested real message IDs."""

        self.store.require_member(db, project, user)
        requested = []
        for item in messages or []:
            message_id = item.get("id") if isinstance(item, dict) else item
            if type(message_id) is int and message_id > 0 and message_id not in requested:
                requested.append(message_id)
        ids = requested
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        cards = {}
        rows = db.execute(
            f"SELECT c.*,m.conversation_id AS message_conversation FROM project_work_cards c "
            f"JOIN messages m ON m.id=c.message_id AND m.project_id=c.project_id "
            f"WHERE c.project_id=? AND c.message_id IN ({placeholders})",
            (project, *ids),
        ).fetchall()
        for card in rows:
            try:
                self.store._conversation_access(db, project, user, card["message_conversation"])
                table = "project_work_drafts" if card["kind"] == "draft" else "project_work_items"
                record = db.execute(
                    f"SELECT * FROM {table} WHERE id=? AND project_id=?",
                    (card["record_id"], project),
                ).fetchone()
                if record is None or not self._visible(db, record, user):
                    continue
            except PermissionError:
                continue
            cards[card["message_id"]] = {
                "kind": card["kind"],
                "id": card["record_id"],
                "event": card["event"],
            }
        return cards

    def card_for_message(self, db, user, project, message_id):
        return self.project_cards(db, user, project, [message_id]).get(message_id)

    def _reference_allowed(self, db, project, conversation, visibility, message_id):
        row = self._message(db, project, message_id)
        allowed = (
            {self.store.group_conversation(project)}
            if visibility == "group"
            else {self.store.group_conversation(project), conversation}
        )
        if row["conversation_id"] not in allowed:
            raise PermissionError("引用消息对任务受众不可见")
        return row

    def _agent(self, db, project, agent_id, *, owner=None):
        row = db.execute(
            "SELECT * FROM agents WHERE id=? AND project_id=? AND enabled=1 AND configuring=0",
            (agent_id, project),
        ).fetchone()
        if row is None or (owner is not None and row["owner_id"] != owner):
            raise PermissionError("只能把工作交给有权控制的可用 Agent")
        return dict(row)

    @staticmethod
    def _clean_contract(data):
        result = {}
        for key in FIELDS:
            value = data.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > 16000:
                raise ValueError("工作目标、交付、验收与验证不能为空且不得过长")
            result[key] = value.strip()
        stage = data.get("stage", "discuss")
        if stage not in {"discuss", "implement"}:
            raise ValueError("工作阶段只能是 discuss 或 implement")
        allowed = data.get("allowed_files", "")
        if not isinstance(allowed, str) or len(allowed) > 16000:
            raise ValueError("文件范围无效")
        if stage == "implement" and not allowed.strip():
            raise ValueError("实现工作需要明确文件所有权")
        visibility = data.get("visibility", "group")
        if visibility not in {"group", "private"}:
            raise ValueError("工作可见性只能是 group 或 private")
        agent = data.get("agent")
        if not isinstance(agent, str) or not agent:
            raise ValueError("需要指定执行 Agent")
        result.update(stage=stage, allowed_files=allowed.strip(), visibility=visibility, agent=agent)
        return result

    def _visible(self, db, row, user):
        try:
            self.store.require_member(db, row["project_id"], user)
        except PermissionError:
            return False
        if row["visibility"] == "group":
            return True
        try:
            self.store._conversation_access(db, row["project_id"], user, row["conversation_id"])
        except PermissionError:
            return False
        return True

    def _may_manage(self, db, row, user, delegate=None):
        if not self._visible(db, row, user):
            return False
        return row["creator_id"] == user or (
            row["visibility"] == "group"
            and db.execute("SELECT 1 FROM projects WHERE id=? AND owner_id=?", (row["project_id"], user)).fetchone()
            is not None
        )

    def _idempotent(self, db, user, project, request_id, payload):
        request_id = self._request_id(request_id)
        digest = self._hash(payload)
        row = db.execute(
            "SELECT * FROM project_work_requests WHERE project_id=? AND actor_id=? AND client_request_id=?",
            (project, user, request_id),
        ).fetchone()
        if row and row["payload_hash"] != digest:
            raise ValueError("工作请求幂等标识已用于其他内容")
        return request_id, digest, row

    def _remember(self, db, project, user, request_id, digest, kind, record_id, revision=None):
        db.execute(
            "INSERT INTO project_work_requests(project_id,actor_id,client_request_id,payload_hash,result_kind,"
            "result_id,result_revision) VALUES(?,?,?,?,?,?,?)",
            (project, user, request_id, digest, kind, record_id, revision),
        )

    def _repeat(self, db, row, user):
        if row["result_kind"] == "draft":
            return self.draft_for_actor(db, user, row["project_id"], row["result_id"], row["result_revision"])
        return self._work_record(db, user, row["result_id"])

    def _conversation(self, db, project, creator, visibility, supplied=None, agent_id=None):
        if visibility == "group":
            conversation = self.store.group_conversation(project)
        else:
            conversation = supplied or self.store._pair_conversation("human_agent", creator, agent_id)
        self.store._conversation_access(db, project, creator, conversation)
        if visibility == "group" and conversation != self.store.group_conversation(project):
            raise ValueError("公开工作必须属于项目群聊")
        if visibility == "private" and conversation != self.store._pair_conversation("human_agent", creator, agent_id):
            raise ValueError("私有工作必须属于创建者与执行 Agent 的私聊")
        return conversation

    def command(self, user, payload, delegate=None):
        project_id = payload.get("project")
        action = payload.get("action")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            project = self.store.require_member(db, project_id, user)
            request_id, digest, prior = self._idempotent(
                db, user, project_id, payload.get("client_request_id"), payload
            )
            if prior:
                return self._repeat(db, prior, user)
            confirm_was_new = True
            if action == "confirm" or (
                action == "draft-discuss" and payload.get("body", "").strip() in {"按这一版创建", "确认创建"}
            ):
                draft_status = db.execute(
                    "SELECT status FROM project_work_drafts WHERE id=? AND project_id=?",
                    (payload.get("draft_id"), project_id),
                ).fetchone()
                confirm_was_new = draft_status is not None and draft_status["status"] == "draft"
            if action == "create":
                record = self._create(db, project, user, payload, delegate)
                kind, revision = "work", None
            elif action == "revise":
                record = self._revise(db, project, user, payload)
                kind, revision = "draft", record["revision"]
            elif action == "confirm":
                record = self._confirm(db, project, user, payload)
                kind, revision = "work", None
            elif action == "draft-discuss":
                record, kind, revision = self._discuss(db, project, user, payload, delegate)
            elif action in {"follow-up", "reference", "accept", "reopen", "update-scope"}:
                record = self._mutate_work(db, project, user, payload, action, delegate)
                kind, revision = "work", None
            else:
                raise ValueError("未知项目工作操作")
            card_events = {
                "create": "created",
                "revise": "draft_revised",
                "confirm": "confirmed",
                "follow-up": "follow_up",
                "accept": "accepted",
                "reopen": "reopened",
                "update-scope": "scope_updated",
            }
            card_event = "confirmed" if action == "draft-discuss" and kind == "work" else card_events.get(action)
            if card_event and not (card_event == "confirmed" and not confirm_was_new):
                card_row = db.execute(
                    "SELECT * FROM project_work_drafts WHERE id=?"
                    if kind == "draft"
                    else "SELECT * FROM project_work_items WHERE id=?",
                    (record["id"],),
                ).fetchone()
                self._publish_card(db, card_row, kind, card_event, revision)
            self._remember(db, project_id, user, request_id, digest, kind, record["id"], revision)
            if record.get("visibility") == "private":
                self.store.emit_conversation_event(db, project_id, record["conversation_id"], "work")
            else:
                self.store.emit_project_event(db, project_id, "work")
            audit = {"id": record["id"]}
            if delegate is not None:
                audit["delegate"] = hashlib.sha256(delegate.encode()).hexdigest()
            if kind == "work" and record.get("draft_id"):
                audit.update(draft=record["draft_id"], revision=record["draft_revision"])
            self.store.log(db, user, "project_work_" + action, audit)
            return record

    def _create(self, db, project, user, data, delegate):
        contract = self._clean_contract(data)
        # Direct human and delegated creation retain the established owner boundary.
        self._agent(db, project["id"], contract["agent"], owner=user)
        conversation = self._conversation(
            db, project["id"], user, contract["visibility"], data.get("conversation_id"), contract["agent"]
        )
        references = list(dict.fromkeys(data.get("reference_message_ids") or []))
        if data.get("source_message_id") is not None:
            references.insert(0, data["source_message_id"])
        for message_id in references:
            self._reference_allowed(db, project["id"], conversation, contract["visibility"], message_id)
        work_id = uid("work")
        now = time.time()
        db.execute(
            "INSERT INTO project_work_items(id,project_id,draft_id,draft_revision,creator_id,conversation_id,"
            "visibility,title,instructions,deliverable,acceptance,validation,agent_id,allowed_files,stage,revision,"
            "status,status_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
            (
                work_id,
                project["id"],
                None,
                None,
                user,
                conversation,
                contract["visibility"],
                contract["title"],
                contract["instructions"],
                contract["deliverable"],
                contract["acceptance"],
                contract["validation"],
                contract["agent"],
                contract["allowed_files"],
                contract["stage"],
                1,
                "queued",
                now,
                now,
            ),
        )
        self._insert_work_revision(db, work_id, 1, contract, user, now)
        for message_id in references:
            db.execute(
                "INSERT INTO project_work_inputs(work_id,kind,message_id,actor_id,created_at) "
                "VALUES(?,'reference',?,?,?)",
                (work_id, message_id, user, now),
            )
        return self._work_record(db, user, work_id)

    def _draft_row(self, db, project, draft_id):
        row = db.execute(
            "SELECT * FROM project_work_drafts WHERE id=? AND project_id=?", (draft_id, project)
        ).fetchone()
        if row is None:
            raise PermissionError("无权访问此工作草稿")
        return row

    def draft_for_actor(self, db, user, project, draft_id, revision=None):
        draft = self._draft_row(db, project, draft_id)
        if not self._visible(db, draft, user):
            raise PermissionError("无权访问此工作草稿")
        revision = revision or draft["current_revision"]
        row = db.execute(
            "SELECT * FROM project_work_draft_revisions WHERE draft_id=? AND revision=?",
            (draft_id, revision),
        ).fetchone()
        if row is None:
            raise ValueError("草稿版本不存在")
        result = dict(row)
        result.update(
            id=draft["id"],
            status=draft["status"],
            creator_id=draft["creator_id"],
            conversation_id=draft["conversation_id"],
            visibility=draft["visibility"],
            agent=result.pop("agent_id"),
            reference_message_ids=self._safe_references(
                db,
                project,
                draft["conversation_id"],
                draft["visibility"],
                [
                    x[0]
                    for x in db.execute(
                        "SELECT message_id FROM project_work_draft_references WHERE draft_id=? AND revision=? ORDER BY message_id",
                        (draft_id, revision),
                    )
                ],
            ),
            can_edit=draft["status"] == "draft" and draft["creator_id"] == user,
            can_manage=draft["creator_id"] == user,
        )
        result.pop("draft_id", None)
        result.pop("created_by", None)
        result.pop("created_at", None)
        return result

    def _revise(self, db, project, user, data):
        draft = self._draft_row(db, project["id"], data.get("draft_id"))
        if draft["status"] != "draft" or draft["creator_id"] != user:
            raise PermissionError("只有草稿创建者可以修改未确认草稿")
        expected = data.get("revision")
        if type(expected) is not int or expected != draft["current_revision"]:
            raise ValueError("草稿版本已更新，请重新查看")
        current = dict(
            db.execute(
                "SELECT * FROM project_work_draft_revisions WHERE draft_id=? AND revision=?",
                (draft["id"], expected),
            ).fetchone()
        )
        merged = {**current, **{key: value for key, value in data.items() if value is not None}}
        merged["agent"] = merged.get("agent", current["agent_id"])
        merged["visibility"] = draft["visibility"]
        contract = self._clean_contract(merged)
        self._agent(
            db,
            project["id"],
            contract["agent"],
            owner=user if draft["visibility"] == "private" or contract["agent"] != current["agent_id"] else None,
        )
        references = data.get("reference_message_ids")
        if references is None:
            references = [
                x[0]
                for x in db.execute(
                    "SELECT message_id FROM project_work_draft_references WHERE draft_id=? AND revision=?",
                    (draft["id"], expected),
                )
            ]
        references = list(dict.fromkeys(references))
        for message_id in references:
            self._reference_allowed(db, project["id"], draft["conversation_id"], draft["visibility"], message_id)
        revision = expected + 1
        self._insert_revision(db, draft["id"], revision, contract, user, references)
        db.execute(
            "UPDATE project_work_drafts SET current_revision=?,updated_at=? WHERE id=?",
            (revision, time.time(), draft["id"]),
        )
        return self.draft_for_actor(db, user, project["id"], draft["id"], revision)

    def _confirm(self, db, project, user, data):
        draft = self._draft_row(db, project["id"], data.get("draft_id"))
        revision = data.get("revision")
        if draft["creator_id"] != user:
            raise PermissionError("只有草稿创建者可以确认")
        if type(revision) is not int or revision != draft["current_revision"]:
            raise ValueError("必须确认当前明确版本")
        if draft["status"] == "confirmed":
            return self._work_record(db, user, draft["confirmed_work_id"])
        spec = db.execute(
            "SELECT * FROM project_work_draft_revisions WHERE draft_id=? AND revision=?",
            (draft["id"], revision),
        ).fetchone()
        prepared_by_current_admin = (
            draft["prepared_by_admin"]
            and db.execute(
                "SELECT 1 FROM project_work_draft_revisions WHERE draft_id=? AND revision=1 AND created_by=?",
                (draft["id"], project.get("admin_agent_id")),
            ).fetchone()
        )
        self._agent(
            db,
            project["id"],
            spec["agent_id"],
            owner=user if draft["visibility"] == "private" or not prepared_by_current_admin else None,
        )
        refs = [
            x[0]
            for x in db.execute(
                "SELECT message_id FROM project_work_draft_references WHERE draft_id=? AND revision=?",
                (draft["id"], revision),
            )
        ]
        for message_id in refs:
            self._reference_allowed(db, project["id"], draft["conversation_id"], draft["visibility"], message_id)
        work_id = uid("work")
        now = time.time()
        db.execute(
            "INSERT INTO project_work_items(id,project_id,draft_id,draft_revision,creator_id,conversation_id,"
            "visibility,title,instructions,deliverable,acceptance,validation,agent_id,allowed_files,stage,revision,"
            "status,status_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
            (
                work_id,
                project["id"],
                draft["id"],
                revision,
                user,
                draft["conversation_id"],
                draft["visibility"],
                spec["title"],
                spec["instructions"],
                spec["deliverable"],
                spec["acceptance"],
                spec["validation"],
                spec["agent_id"],
                spec["allowed_files"],
                spec["stage"],
                1,
                "queued",
                now,
                now,
            ),
        )
        self._insert_work_revision(db, work_id, 1, dict(spec), user, now)
        for message_id in refs:
            db.execute(
                "INSERT INTO project_work_inputs(work_id,kind,message_id,actor_id,created_at) VALUES(?,'reference',?,?,?)",
                (work_id, message_id, user, now),
            )
        db.execute(
            "UPDATE project_work_drafts SET status='confirmed',confirmed_work_id=?,updated_at=? WHERE id=?",
            (work_id, now, draft["id"]),
        )
        return self._work_record(db, user, work_id)

    def _discuss(self, db, project, user, data, delegate):
        draft = self._draft_row(db, project["id"], data.get("draft_id"))
        revision = data.get("revision")
        if not self._visible(db, draft, user) or type(revision) is not int or revision != draft["current_revision"]:
            raise ValueError("请选择当前草稿版本")
        body = data.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > 16000:
            raise ValueError("讨论内容不能为空或过长")
        body = body.strip()
        if body in {"按这一版创建", "确认创建"}:
            record = self._confirm(db, project, user, data)
            return record, "work", None
        admin_id = project.get("admin_agent_id")
        if not admin_id:
            raise ValueError("项目尚未设置管理员 Agent")
        if draft["visibility"] == "group":
            sent = self.store._message(db, project["id"], user, "human", body, None, mention_agent_id=admin_id)
        else:
            admin = self._agent(db, project["id"], admin_id, owner=user)
            sent = self.store._message(db, project["id"], user, "human", body, admin["id"])
        db.execute(
            "INSERT INTO project_work_draft_discussion VALUES(?,?,?)",
            (sent["id"], draft["id"], revision),
        )
        return self.draft_for_actor(db, user, project["id"], draft["id"], revision), "draft", revision

    def _mutate_work(self, db, project, user, data, action, delegate):
        row = db.execute(
            "SELECT * FROM project_work_items WHERE id=? AND project_id=?",
            (data.get("work_id"), project["id"]),
        ).fetchone()
        if row is None or not self._may_manage(db, row, user, delegate):
            raise PermissionError("无权管理此工作")
        now = time.time()
        if action == "update-scope":
            if row["status"] == "accepted":
                raise ValueError("已验收工作必须先重新打开")
            expected = data.get("revision")
            if type(expected) is not int or expected != row["revision"]:
                raise ValueError("工作范围版本已更新，请重新查看")
            changes = {key: data[key] for key in (*FIELDS, "allowed_files") if key in data}
            if not changes:
                raise ValueError("没有可更新的工作范围")
            merged = {**dict(row), **changes, "agent": row["agent_id"]}
            contract = self._clean_contract(merged)
            revision = expected + 1
            self._insert_work_revision(db, row["id"], revision, contract, user, now)
            db.execute(
                "UPDATE project_work_items SET title=?,instructions=?,deliverable=?,acceptance=?,validation=?,"
                "allowed_files=?,revision=?,updated_at=? WHERE id=?",
                (
                    contract["title"],
                    contract["instructions"],
                    contract["deliverable"],
                    contract["acceptance"],
                    contract["validation"],
                    contract["allowed_files"],
                    revision,
                    now,
                    row["id"],
                ),
            )
            db.execute(
                "INSERT INTO project_work_inputs(work_id,kind,body,actor_id,created_at) VALUES(?,'scope-update',?,?,?)",
                (row["id"], json.dumps({"revision": revision}, separators=(",", ":")), user, now),
            )
            if row["status"] in {"delivered", "blocked", "waiting"}:
                db.execute("UPDATE project_work_items SET status='queued',status_reason=NULL WHERE id=?", (row["id"],))
        elif action == "accept":
            if row["status"] != "delivered":
                raise ValueError("只有已交付工作可以验收")
            db.execute("UPDATE project_work_items SET status='accepted',updated_at=? WHERE id=?", (now, row["id"]))
        elif action == "reopen":
            if row["status"] not in {"accepted", "delivered", "blocked"}:
                raise ValueError("当前工作状态不能重新打开")
            body = data.get("body")
            if not isinstance(body, str) or not body.strip() or len(body) > 16000:
                raise ValueError("重新打开需要说明")
            db.execute(
                "INSERT INTO project_work_inputs(work_id,kind,body,actor_id,created_at) VALUES(?,'reopen',?,?,?)",
                (row["id"], body.strip(), user, now),
            )
            db.execute(
                "UPDATE project_work_items SET status='queued',status_reason=NULL,updated_at=? WHERE id=?",
                (now, row["id"]),
            )
        elif action == "follow-up":
            if row["status"] == "accepted":
                raise ValueError("已验收工作必须先重新打开")
            body = data.get("body")
            if not isinstance(body, str) or not body.strip() or len(body) > 16000:
                raise ValueError("跟进内容不能为空")
            db.execute(
                "INSERT INTO project_work_inputs(work_id,kind,body,actor_id,created_at) VALUES(?,'follow-up',?,?,?)",
                (row["id"], body.strip(), user, now),
            )
            if row["status"] in {"delivered", "blocked", "waiting"}:
                db.execute(
                    "UPDATE project_work_items SET status='queued',status_reason=NULL,updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
        else:
            message_id = data.get("message_id")
            self._reference_allowed(db, project["id"], row["conversation_id"], row["visibility"], message_id)
            db.execute(
                "INSERT OR IGNORE INTO project_work_inputs(work_id,kind,message_id,actor_id,created_at) "
                "VALUES(?,'reference',?,?,?)",
                (row["id"], message_id, user, now),
            )
        return self._work_record(db, user, row["id"])

    @staticmethod
    def _insert_revision(db, draft_id, revision, spec, actor, references):
        db.execute(
            "INSERT INTO project_work_draft_revisions(draft_id,revision,title,instructions,deliverable,acceptance,"
            "validation,agent_id,allowed_files,stage,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                draft_id,
                revision,
                spec["title"],
                spec["instructions"],
                spec["deliverable"],
                spec["acceptance"],
                spec["validation"],
                spec["agent"],
                spec["allowed_files"],
                spec["stage"],
                actor,
                time.time(),
            ),
        )
        db.executemany(
            "INSERT INTO project_work_draft_references VALUES(?,?,?)",
            [(draft_id, revision, item) for item in references],
        )

    @staticmethod
    def _insert_work_revision(db, work_id, revision, spec, actor, created_at=None):
        db.execute(
            "INSERT INTO project_work_item_revisions(work_id,revision,title,instructions,deliverable,acceptance,"
            "validation,allowed_files,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                work_id,
                revision,
                spec["title"],
                spec["instructions"],
                spec["deliverable"],
                spec["acceptance"],
                spec["validation"],
                spec["allowed_files"],
                actor,
                time.time() if created_at is None else created_at,
            ),
        )

    def propose(self, db, project, admin, source, proposal):
        project_id = project["id"] if isinstance(project, dict) else project
        admin_row = (
            admin
            if hasattr(admin, "keys")
            else db.execute("SELECT * FROM agents WHERE id=? AND project_id=?", (admin, project_id)).fetchone()
        )
        project_row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        source_row = source if hasattr(source, "keys") else self._message(db, project_id, source)
        if (
            admin_row is None
            or project_row is None
            or project_row["admin_agent_id"] != admin_row["id"]
            or source_row["project_id"] != project_id
            or source_row["sender_type"] != "human"
            or not (
                source_row["recipient_id"] == admin_row["id"]
                or source_row["recipient_id"] is None
                and source_row["mention_agent_id"] == admin_row["id"]
            )
        ):
            raise PermissionError("只有当前管理员可从人类消息准备草稿")
        if not isinstance(proposal, dict):
            raise ValueError("工作建议必须是一个草稿")
        conversation = source_row["conversation_id"]
        visibility = "group" if conversation == self.store.group_conversation(project_id) else "private"
        if visibility == "private":
            expected = self.store._pair_conversation("human_agent", source_row["sender_id"], admin_row["id"])
            if conversation != expected or admin_row["owner_id"] != source_row["sender_id"]:
                raise PermissionError("管理员无权读取此来源消息")
        spec = self._clean_contract({**proposal, "visibility": visibility})
        if visibility == "private" and spec["agent"] != admin_row["id"]:
            raise PermissionError("私聊中准备的工作只能由该管理员 Agent 执行")
        self._agent(db, project_id, spec["agent"], owner=source_row["sender_id"] if visibility == "private" else None)
        draft_id, expected = proposal.get("draft_id") or proposal.get("id"), proposal.get("revision")
        pin = db.execute(
            "SELECT draft_id,revision FROM project_work_draft_discussion WHERE message_id=?", (source_row["id"],)
        ).fetchone()
        if pin:
            if draft_id not in (None, pin["draft_id"]) or (
                expected is not None
                and (type(expected) is not int or expected not in (pin["revision"], pin["revision"] + 1))
            ):
                raise ValueError("此消息只能修订它所关联的草稿版本")
            # The human's bound message determines the base revision. A model
            # sometimes labels its proposal with the next revision it expects.
            draft_id, expected = pin["draft_id"], pin["revision"]
        if draft_id is not None and not isinstance(draft_id, str):
            raise ValueError("草稿编号无效")
        if (
            not pin
            and (expected is None or type(expected) is int and expected == 1)
            and draft_id
            and not re.fullmatch(r"draft-[a-f0-9]{32}", draft_id)
            and not db.execute(
                "SELECT 1 FROM project_work_drafts WHERE project_id=? AND creator_id=? AND conversation_id=? "
                "AND status='draft'",
                (project_id, source_row["sender_id"], conversation),
            ).fetchone()
        ):
            # Some executors label a first proposal with a placeholder ID. It is
            # never authoritative and can only be discarded for an unbound new draft.
            draft_id, expected = None, None
        earlier = []
        if draft_id:
            draft = self._draft_row(db, project_id, draft_id)
            if (
                draft["status"] != "draft"
                or not draft["prepared_by_admin"]
                or draft["creator_id"] != source_row["sender_id"]
                or draft["conversation_id"] != conversation
                or type(expected) is not int
                or expected != draft["current_revision"]
            ):
                raise ValueError("管理员只能基于当前明确版本修改同一来源草稿")
            earlier = [
                row[0]
                for row in db.execute(
                    "SELECT message_id FROM project_work_draft_references WHERE draft_id=? AND revision=? ORDER BY message_id",
                    (draft_id, expected),
                )
            ]
        elif expected is not None:
            raise ValueError("新草稿不能声明既有版本")
        added = proposal.get("reference_message_ids") or []
        if not isinstance(added, list) or len(added) > 30:
            raise ValueError("草稿引用无效")
        references = list(dict.fromkeys([*earlier, source_row["id"], *added]))
        for message_id in references:
            self._reference_allowed(db, project_id, conversation, visibility, message_id)
        now = time.time()
        if draft_id:
            revision = expected + 1
            db.execute(
                "UPDATE project_work_drafts SET current_revision=?,updated_at=? WHERE id=?",
                (revision, now, draft_id),
            )
        else:
            draft_id, revision = uid("draft"), 1
            db.execute(
                "INSERT INTO project_work_drafts(id,project_id,creator_id,conversation_id,visibility,status,"
                "current_revision,prepared_by_admin,confirmed_work_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'draft',1,1,NULL,?,?)",
                (draft_id, project_id, source_row["sender_id"], conversation, visibility, now, now),
            )
        self._insert_revision(db, draft_id, revision, spec, admin_row["id"], references)
        card_row = db.execute("SELECT * FROM project_work_drafts WHERE id=?", (draft_id,)).fetchone()
        self._publish_card(db, card_row, "draft", "draft_saved" if revision == 1 else "draft_revised", revision)
        self.store.log(
            db,
            admin_row["id"],
            "project_work_proposal",
            {
                "draft": draft_id,
                "revision": revision,
                "source_message": source_row["id"],
            },
        )
        self.store.emit_conversation_event(db, project_id, conversation, "work")
        return self.draft_for_actor(db, source_row["sender_id"], project_id, draft_id, revision)

    def agent_drafts(self, db, project, admin_id, source):
        project_id = project["id"] if isinstance(project, dict) else project
        source_row = source if hasattr(source, "keys") else self._message(db, project_id, source)
        current = db.execute("SELECT admin_agent_id FROM projects WHERE id=?", (project_id,)).fetchone()
        if current is None or current["admin_agent_id"] != admin_id:
            return []
        selected = db.execute(
            "SELECT draft_id,revision FROM project_work_draft_discussion WHERE message_id=?",
            (source_row["id"],),
        ).fetchone()
        if selected:
            try:
                return [
                    self.draft_for_actor(
                        db, source_row["sender_id"], project_id, selected["draft_id"], selected["revision"]
                    )
                ]
            except (PermissionError, ValueError):
                return []
        rows = db.execute(
            "SELECT * FROM project_work_drafts WHERE project_id=? AND creator_id=? AND conversation_id=? "
            "AND status='draft' ORDER BY updated_at DESC",
            (project_id, source_row["sender_id"], source_row["conversation_id"]),
        ).fetchall()
        return [self.draft_for_actor(db, source_row["sender_id"], project_id, row["id"]) for row in rows]

    def agent_work(self, db, project, admin, source):
        """Return actionable work the speaking human can explicitly follow up."""

        project_id = project["id"] if isinstance(project, dict) else project
        admin_row = (
            admin
            if hasattr(admin, "keys")
            else db.execute("SELECT * FROM agents WHERE id=? AND project_id=?", (admin, project_id)).fetchone()
        )
        source_row = source if hasattr(source, "keys") else self._message(db, project_id, source)
        current = db.execute("SELECT admin_agent_id FROM projects WHERE id=?", (project_id,)).fetchone()
        if (
            admin_row is None
            or current is None
            or current["admin_agent_id"] != admin_row["id"]
            or source_row["sender_type"] != "human"
            or not (
                source_row["recipient_id"] == admin_row["id"]
                or source_row["recipient_id"] is None
                and source_row["mention_agent_id"] == admin_row["id"]
            )
        ):
            return []
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "agent": row["agent_id"],
                "conversation_id": row["conversation_id"],
                "can_follow_up": row["status"] != "accepted",
                "requires_reopen": row["status"] == "accepted",
            }
            for row in db.execute(
                "SELECT * FROM project_work_items WHERE project_id=? AND creator_id=? "
                "AND conversation_id=? ORDER BY updated_at DESC",
                (project_id, source_row["sender_id"], source_row["conversation_id"]),
            )
        ]

    def propose_follow_up(self, db, project, admin, source, work_id, instruction=None):
        """Persist the human's exact addressed message as an idempotent follow-up."""

        project_id = project["id"] if isinstance(project, dict) else project
        source_row = source if hasattr(source, "keys") else self._message(db, project_id, source)
        candidates = {item["id"]: item for item in self.agent_work(db, project, admin, source_row)}
        selected = candidates.get(work_id)
        if selected is None or not selected["can_follow_up"]:
            raise PermissionError("此消息不能跟进该工作")
        work = db.execute("SELECT * FROM project_work_items WHERE id=?", (work_id,)).fetchone()
        self._reference_allowed(db, project_id, work["conversation_id"], work["visibility"], source_row["id"])
        request_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"foolship:work-follow-up:{project_id}:{work_id}:{source_row['id']}")
        )
        existing = db.execute(
            "SELECT result_id FROM project_work_requests WHERE project_id=? AND actor_id=? AND client_request_id=?",
            (project_id, source_row["sender_id"], request_id),
        ).fetchone()
        if existing:
            return self._work_record(db, source_row["sender_id"], work_id)
        body = source_row["body"]
        if instruction is not None:
            if (
                not isinstance(instruction, str)
                or not instruction.strip()
                or len(instruction) > 16000
                or instruction not in body
            ):
                raise ValueError("跟进指令必须是来源消息中的非空连续原文")
            body = instruction
        now = time.time()
        db.execute(
            "INSERT INTO project_work_inputs(work_id,kind,body,actor_id,created_at) VALUES(?,'follow-up',?,?,?)",
            (work_id, body, source_row["sender_id"], now),
        )
        db.execute(
            "INSERT INTO project_work_inputs(work_id,kind,message_id,actor_id,created_at) VALUES(?,'reference',?,?,?)",
            (work_id, source_row["id"], source_row["sender_id"], now),
        )
        if work["status"] in {"delivered", "blocked", "waiting"}:
            db.execute("UPDATE project_work_items SET status='queued',updated_at=? WHERE id=?", (now, work_id))
        digest = self._hash({"source_message_id": source_row["id"], "work_id": work_id})
        self._remember(db, project_id, source_row["sender_id"], request_id, digest, "work", work_id)
        self._publish_card(db, work, "work", "follow_up")
        self.store.emit_conversation_event(db, project_id, work["conversation_id"], "work")
        return self._work_record(db, source_row["sender_id"], work_id)

    def _safe_references(self, db, project, conversation, visibility, ids):
        result = []
        for message_id in ids:
            try:
                self._reference_allowed(db, project, conversation, visibility, message_id)
            except PermissionError:
                continue
            result.append(message_id)
        return result

    def _work_record(self, db, user, work_id):
        row = db.execute("SELECT * FROM project_work_items WHERE id=?", (work_id,)).fetchone()
        if row is None or not self._visible(db, row, user):
            raise PermissionError("无权访问此工作")
        result = dict(row)
        result["agent"] = result.pop("agent_id")
        inputs = []
        for item in db.execute("SELECT * FROM project_work_inputs WHERE work_id=? ORDER BY id", (work_id,)):
            value = dict(item)
            if value["message_id"] is not None:
                try:
                    self._reference_allowed(
                        db, row["project_id"], row["conversation_id"], row["visibility"], value["message_id"]
                    )
                except PermissionError:
                    continue
            inputs.append(value)
        result["inputs"] = inputs
        result["executions"] = [
            dict(x)
            for x in db.execute(
                "SELECT e.*,t.state AS task_state FROM project_work_executions e JOIN tasks t ON t.id=e.task_id "
                "WHERE e.work_id=? ORDER BY e.created_at",
                (work_id,),
            )
        ]
        if (
            result["status"] == "running"
            and result["executions"]
            and result["executions"][-1]["task_state"] == "unknown"
        ):
            result["status_reason"] = "原设备的执行状态待确认。确认前此任务不会重复派工。"
        result["deliveries"] = [
            {**dict(x), "links": json.loads(x["links"])}
            for x in db.execute("SELECT * FROM project_work_deliveries WHERE work_id=? ORDER BY id", (work_id,))
        ]
        result["scope_history"] = [
            dict(x)
            for x in db.execute(
                "SELECT revision,title,instructions,deliverable,acceptance,validation,allowed_files,actor_id,created_at "
                "FROM project_work_item_revisions WHERE work_id=? ORDER BY revision",
                (work_id,),
            )
        ]
        result["can_manage"] = self._may_manage(db, row, user)
        return result

    def view(self, db, user, project):
        self.store.require_member(db, project, user)
        drafts = []
        for row in db.execute(
            "SELECT * FROM project_work_drafts WHERE project_id=? ORDER BY updated_at DESC", (project,)
        ):
            if self._visible(db, row, user):
                drafts.append(self.draft_for_actor(db, user, project, row["id"]))
        items = []
        for row in db.execute(
            "SELECT id FROM project_work_items WHERE project_id=? ORDER BY updated_at DESC", (project,)
        ):
            try:
                items.append(self._work_record(db, user, row["id"]))
            except PermissionError:
                pass
        return {"drafts": drafts, "items": items}

    def schedule(self, db):
        scheduled = []
        rows = db.execute("SELECT * FROM project_work_items WHERE status='queued' ORDER BY created_at").fetchall()
        for work in rows:
            project = dict(db.execute("SELECT * FROM projects WHERE id=?", (work["project_id"],)).fetchone())
            agent = db.execute(
                "SELECT * FROM agents WHERE id=? AND project_id=?", (work["agent_id"], work["project_id"])
            ).fetchone()
            member = (
                agent
                and db.execute(
                    "SELECT 1 FROM project_users WHERE project_id=? AND user_id=?",
                    (work["project_id"], agent["owner_id"]),
                ).fetchone()
            )
            if agent is None or not member or not agent["enabled"] or agent["configuring"]:
                reason = (
                    "执行 Agent 已不存在或其所有者已离开项目"
                    if agent is None or not member
                    else "执行 Agent 当前未启用"
                )
                db.execute(
                    "UPDATE project_work_items SET status='blocked',status_reason=?,updated_at=? WHERE id=?",
                    (reason, time.time(), work["id"]),
                )
                continue
            execution_id = uid("work-execution")
            scope = db.execute(
                "SELECT * FROM project_work_item_revisions WHERE work_id=? AND revision=?",
                (work["id"], work["revision"]),
            ).fetchone()
            if scope is None:
                db.execute(
                    "UPDATE project_work_items SET status='blocked',status_reason=?,updated_at=? WHERE id=?",
                    ("工作范围版本缺失，请重新创建工作", time.time(), work["id"]),
                )
                continue
            contract = {
                "title": scope["title"],
                "instructions": scope["instructions"],
                "deliverable": scope["deliverable"],
                "acceptance": scope["acceptance"],
                "validation": scope["validation"],
                "agent": work["agent_id"],
                "allowed_files": scope["allowed_files"],
                "stage": work["stage"],
                "project_work_execution_id": execution_id,
                "project_work_revision": work["revision"],
                "project_action_v2": True,
            }
            if work["visibility"] == "private":
                contract.update(private_turn=True, reply_to=work["creator_id"], audience="private")
            source = next(
                (
                    x[0]
                    for x in db.execute(
                        "SELECT message_id FROM project_work_inputs WHERE work_id=? AND kind='reference' ORDER BY id",
                        (work["id"],),
                    )
                    if x[0] is not None
                ),
                None,
            )
            try:
                task = self.store._task(
                    db, project, dict(agent), contract, work["creator_id"], source_message_id=source
                )
            except ValueError:
                continue
            now = time.time()
            input_cutoff = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM project_work_inputs WHERE work_id=?", (work["id"],)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO project_work_executions(id,work_id,task_id,state,revision,input_cutoff,created_at,"
                "finished_at) VALUES(?,?,?,?,?,?,?,NULL)",
                (execution_id, work["id"], task["id"], "running", work["revision"], input_cutoff, now),
            )
            db.execute(
                "UPDATE project_work_items SET status='running',status_reason=NULL,updated_at=? WHERE id=?",
                (now, work["id"]),
            )
            scheduled.append(
                {
                    "id": execution_id,
                    "work_id": work["id"],
                    "task_id": task["id"],
                    "revision": work["revision"],
                    "state": "running",
                }
            )
        return scheduled

    def execution_context(self, db, execution_id):
        row = db.execute(
            "SELECT e.id AS execution_id,e.work_id,e.revision AS execution_revision,e.input_cutoff,"
            "w.project_id,w.conversation_id,w.visibility,w.stage,"
            "s.title,s.instructions,s.deliverable,s.acceptance,s.validation,s.allowed_files "
            "FROM project_work_executions e JOIN project_work_items w ON w.id=e.work_id "
            "JOIN project_work_item_revisions s ON s.work_id=e.work_id AND s.revision=e.revision WHERE e.id=?",
            (execution_id,),
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        refs = self._safe_references(
            db,
            record["project_id"],
            record["conversation_id"],
            record["visibility"],
            [
                x[0]
                for x in db.execute(
                    "SELECT message_id FROM project_work_inputs WHERE work_id=? AND id<=? "
                    "AND kind='reference' ORDER BY id",
                    (record["work_id"], record["input_cutoff"]),
                )
            ],
        )
        followups = [
            dict(x)
            for x in db.execute(
                "SELECT kind,body,actor_id,created_at FROM project_work_inputs "
                "WHERE work_id=? AND id<=? AND kind IN ('follow-up','reopen') ORDER BY id",
                (record["work_id"], record["input_cutoff"]),
            )
        ]
        references = [
            dict(
                db.execute(
                    "SELECT id,sender_id,sender_type,recipient_id,body,created_at,conversation_id "
                    "FROM messages WHERE id=?",
                    (message_id,),
                ).fetchone()
            )
            for message_id in refs
        ]
        return {
            "execution_id": execution_id,
            "work_id": record["work_id"],
            "revision": record["execution_revision"],
            "project": record["project_id"],
            "title": record["title"],
            "instructions": record["instructions"],
            "deliverable": record["deliverable"],
            "acceptance": record["acceptance"],
            "validation": record["validation"],
            "allowed_files": record["allowed_files"],
            "stage": record["stage"],
            "visibility": record["visibility"],
            "conversation_id": record["conversation_id"],
            "reference_message_ids": refs,
            "references": references,
            "follow_ups": followups,
            "previous_deliveries": [
                {**dict(item), "links": json.loads(item["links"])}
                for item in db.execute(
                    "SELECT summary,links,message_id,created_at FROM project_work_deliveries "
                    "WHERE work_id=? ORDER BY id",
                    (record["work_id"],),
                )
            ],
        }

    def finish(self, db, execution_id, state, delivery=None):
        execution = db.execute("SELECT * FROM project_work_executions WHERE id=?", (execution_id,)).fetchone()
        if execution is None or execution["finished_at"] is not None:
            return None
        if state not in WORK_STATUSES - {"accepted"}:
            raise ValueError("未知工作执行状态")
        if delivery is not None:
            if (
                not isinstance(delivery, dict)
                or not isinstance(delivery.get("summary"), str)
                or not delivery["summary"].strip()
                or not isinstance(delivery.get("links"), list)
                or not all(isinstance(x, str) for x in delivery["links"])
                or type(delivery.get("message_id")) is not int
            ):
                raise ValueError("工作交付格式无效")
            work = db.execute("SELECT * FROM project_work_items WHERE id=?", (execution["work_id"],)).fetchone()
            message = self._message(db, work["project_id"], delivery["message_id"])
            if message["conversation_id"] != work["conversation_id"] or message["causal_task"] != execution["task_id"]:
                raise PermissionError("交付消息必须属于本次执行与约定会话")
            db.execute(
                "INSERT INTO project_work_deliveries(work_id,execution_id,summary,links,message_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    work["id"],
                    execution_id,
                    delivery["summary"].strip(),
                    json.dumps(delivery["links"], ensure_ascii=False),
                    delivery["message_id"],
                    time.time(),
                ),
            )
            self.add_work_execution_card(db, execution_id, delivery["message_id"], "delivered")
            state = "delivered"
        now = time.time()
        db.execute(
            "UPDATE project_work_executions SET state=?,finished_at=? WHERE id=?",
            (state, now, execution_id),
        )
        pending = db.execute(
            "SELECT 1 FROM project_work_inputs WHERE work_id=? AND id>? AND kind<>'reference' LIMIT 1",
            (execution["work_id"], execution["input_cutoff"]),
        ).fetchone()
        final_state = "queued" if pending else state
        error = db.execute("SELECT error FROM tasks WHERE id=?", (execution["task_id"],)).fetchone()
        reason = (
            "上一项任务的工作区仍有未提交修改。请先保存其变更，再重新运行此任务。"
            if final_state == "blocked" and error and error["error"] == DIRTY_WORKTREE
            else None
        )
        db.execute(
            "UPDATE project_work_items SET status=?,status_reason=?,updated_at=? WHERE id=?",
            (final_state, reason, now, execution["work_id"]),
        )
        work = db.execute("SELECT * FROM project_work_items WHERE id=?", (execution["work_id"],)).fetchone()
        self.store.emit_conversation_event(db, work["project_id"], work["conversation_id"], "work")
        return {"id": execution["work_id"], "status": final_state}
