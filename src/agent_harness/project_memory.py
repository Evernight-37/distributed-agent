"""Project memory, reviewed candidates and their public sources."""

import json
import time


NOTE_KINDS = ("rule", "decision", "fact")


def _body(value, limit):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"项目记忆需为 1–{limit} 字符")
    return value.strip()


def _kind(value):
    if value is not None and value not in NOTE_KINDS:
        raise ValueError("无效的项目记忆类别")
    return value


class ProjectMemory:
    def __init__(self, store):
        self.store = store

    def _require_delegate(self, db, delegate, project, user):
        if delegate is None:
            return
        row = db.execute(
            """SELECT d.scopes FROM account_delegates d
            JOIN mcp_approval_policies p ON p.owner_id=d.account_id AND p.project_id=? AND p.kind='memory'
            WHERE d.public_key=? AND d.account_id=? AND d.revoked=0""",
            (project, delegate, user),
        ).fetchone()
        scopes = json.loads(row["scopes"]) if row else None
        if not isinstance(scopes, list) or "collaboration.task" not in scopes:
            raise PermissionError("此项目未授权 MCP 代审项目记忆")

    def snapshot(self, user, project):
        with self.store.connect() as db:
            self.store.require_member(db, project, user)
            result = {}
            for key, state in (("notes", "active"), ("candidates", "pending"), ("archived", "archived")):
                rows = db.execute(
                    """SELECT * FROM project_memory_notes WHERE project_id=? AND state=?
                    ORDER BY id DESC LIMIT 100""",
                    (project, state),
                ).fetchall()
                result[key] = [dict(row) for row in reversed(rows)]
            targets = [row["id"] for rows in result.values() for row in rows if row["reviewed_at"] is not None]
            if targets:
                placeholders = ",".join("?" for _ in targets)
                sources = {}
                for row in db.execute(
                    "SELECT id,merged_into,author_id,source_task_id,source_message_id FROM project_memory_notes "
                    f"WHERE project_id=? AND state='archived' AND merged_into IN ({placeholders}) ORDER BY id",
                    (project, *targets),
                ):
                    sources.setdefault(row["merged_into"], []).append(dict(row))
                for rows in result.values():
                    for row in rows:
                        if row["id"] in sources:
                            row["sources"] = sources[row["id"]]
            return result

    def add_note(self, user, project, body, *, kind=None):
        body, kind = _body(body, 2000), _kind(kind)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.require_member(db, project, user, active=True)
            cursor = db.execute(
                """INSERT INTO project_memory_notes(project_id,author_id,body,created_at,kind)
                VALUES(?,?,?,?,?)""",
                (project, user, body, time.time(), kind),
            )
            self.store.emit_project_event(db, project, "memory")
            return {"id": cursor.lastrowid}

    def delete_note(self, user, project, note_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            membership = self.store.require_member(db, project, user, active=True)
            row = db.execute(
                """SELECT author_id FROM project_memory_notes
                WHERE project_id=? AND id=? AND state='active'""",
                (project, note_id),
            ).fetchone()
            if not row:
                raise ValueError("记忆条目不存在")
            if user not in (row["author_id"], membership["owner_id"]):
                raise PermissionError("只有作者或群主可以删除记忆")
            db.execute("DELETE FROM project_memory_notes WHERE project_id=? AND id=?", (project, note_id))
            self.store.emit_project_event(db, project, "memory")
            return {"deleted": True}

    def review_notes(self, user, project, ids, action, *, body=None, kind=None, delegate=None):
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 100
            or any(type(i) is not int or not 1 <= i < 2**63 for i in ids)
        ):
            raise ValueError("请选择有效的记忆条目")
        ids = sorted(set(ids))
        if action not in ("keep", "archive", "merge"):
            raise ValueError("无效的项目记忆操作")
        kind = _kind(kind)
        if action == "merge":
            if len(ids) < 2:
                raise ValueError("至少需要两条候选记忆才能合并")
            body = _body(body, 2000)
        elif body is not None:
            raise ValueError("仅合并时可以填写新的记忆正文")
        elif kind is not None and action == "archive":
            raise ValueError("归档时不能更改记忆类别")
        placeholders = ",".join("?" for _ in ids)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            member = self.store.require_member(db, project, user, active=True)
            self._require_delegate(db, delegate, project, user)
            rows = db.execute(
                "SELECT * FROM project_memory_notes WHERE project_id=? AND state<>'restricted' "
                f"AND id IN ({placeholders}) ORDER BY id",
                (project, *ids),
            ).fetchall()
            if len(rows) != len(ids):
                raise ValueError("记忆条目不存在")
            if action == "merge":
                return self._merge_notes(db, user, project, rows, body, kind, delegate)
            state = "active" if action == "keep" else "archived"
            changes = []
            for row in rows:
                if row["state"] == state:
                    if kind is not None and kind != row["kind"]:
                        raise ValueError("候选已确认，请刷新项目记忆")
                    continue
                if row["state"] == "archived":
                    raise ValueError("记忆候选已归档，请刷新项目记忆")
                if row["state"] == "active" and user not in (row["author_id"], member["owner_id"]):
                    raise PermissionError("只有作者或群主可以归档正式记忆")
                changes.append(row["id"])
            if changes:
                now = time.time()
                db.executemany(
                    """UPDATE project_memory_notes SET state=?,kind=COALESCE(?,kind),reviewed_by=?,reviewed_at=?
                    WHERE project_id=? AND id=?""",
                    [(state, kind, user, now, project, note_id) for note_id in changes],
                )
                self._review_event(db, user, project, action, changes, delegate)
            return {"ids": ids, "state": state}

    def _merge_notes(self, db, user, project, rows, body, kind, delegate):
        if any(row["state"] != "pending" for row in rows):
            target = rows[0]["merged_into"]
            if target and all(row["state"] == "archived" and row["merged_into"] == target for row in rows):
                merged = db.execute(
                    "SELECT body,kind,state FROM project_memory_notes WHERE id=? AND project_id=?", (target, project)
                ).fetchone()
                if merged and merged["state"] == "active" and (merged["body"], merged["kind"]) == (body, kind):
                    return {"ids": [target], "state": "active"}
            raise ValueError("只能合并待确认的记忆候选")
        now = time.time()
        merged = db.execute(
            """INSERT INTO project_memory_notes(project_id,author_id,body,created_at,kind,reviewed_by,reviewed_at)
            VALUES(?,?,?,?,?,?,?)""",
            (project, user, body, now, kind, user, now),
        ).lastrowid
        ids = [row["id"] for row in rows]
        db.executemany(
            """UPDATE project_memory_notes SET state='archived',reviewed_by=?,reviewed_at=?,merged_into=?
            WHERE project_id=? AND id=?""",
            [(user, now, merged, project, note_id) for note_id in ids],
        )
        self._review_event(db, user, project, "merge", ids, delegate, merged=merged)
        return {"ids": [merged], "state": "active"}

    def _review_event(self, db, user, project, action, ids, delegate, *, merged=None):
        detail = {"project": project, "action": action, "ids": ids}
        if delegate is not None:
            detail["delegate"] = delegate
        if merged is not None:
            detail["merged"] = merged
        self.store.log(db, user, "project_memory_review", detail)
        self.store.emit_project_event(db, project, "memory")

    def agent_context(self, project, *, limit=12000):
        """Return a bounded view of approved project memory."""
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT id,author_id,body,created_at,kind FROM project_memory_notes
                WHERE project_id=? AND state='active'
                ORDER BY COALESCE(reviewed_at,created_at) DESC,id DESC LIMIT 100""",
                (project,),
            ).fetchall()
        selected = []
        size = 0
        for row in rows:
            item = dict(row)
            cost = len(item["body"])
            if size + cost > limit:
                break
            selected.append(item)
            size += cost
        return list(reversed(selected))

    def add_agent_notes(self, db, agent, project, task, notes, *, reply_message_id=None):
        if not isinstance(notes, list) or len(notes) > 3:
            raise ValueError("项目记忆条目过多")
        bodies = list(dict.fromkeys(_body(note, 500) for note in notes))
        record = db.execute("SELECT admin_agent_id,archived_at FROM projects WHERE id=?", (project,)).fetchone()
        if not record or record["admin_agent_id"] != agent or record["archived_at"] is not None:
            return
        contract = json.loads(task["contract"])
        if contract.get("private_turn") or contract.get("reply_to") is not None:
            return
        trace = db.execute(
            "SELECT source_message_id,wake_source_id FROM task_trace WHERE task_id=?", (task["id"],)
        ).fetchone()
        source_message = None
        group = self.store.group_conversation(project)
        for message_id in (*trace, reply_message_id) if trace else (reply_message_id,):
            if message_id is None:
                continue
            message = db.execute(
                "SELECT recipient_id,conversation_id,causal_task FROM messages WHERE id=? AND project_id=?",
                (message_id, project),
            ).fetchone()
            if (
                not message
                or message["recipient_id"] is not None
                or message["conversation_id"] not in (None, group)
                or (message["causal_task"] or "").startswith("draft:")
            ):
                return
            if source_message is None:
                source_message = message_id
        changed = False
        for body in bodies:
            cursor = db.execute(
                """INSERT INTO project_memory_notes
                (project_id,author_id,body,created_at,source_task_id,state,source_message_id)
                SELECT ?,?,?,?,?,'pending',? WHERE NOT EXISTS(
                    SELECT 1 FROM project_memory_notes WHERE project_id=? AND body=?
                    AND (state IN ('active','pending') OR source_task_id=?))""",
                (project, agent, body, time.time(), task["id"], source_message, project, body, task["id"]),
            )
            changed |= cursor.rowcount > 0
        if changed:
            self.store.emit_project_event(db, project, "memory")
