"""Owner-only archival, restoration and deletion of shared project records."""

import time


# Delete dependent rows before their parent rows. Work revisions, deliveries,
# tool receipts, and task traces are removed by their foreign keys.
PROJECT_TABLES = (
    "project_work_items",
    "project_work_drafts",
    "project_work_requests",
    "project_work_cards",
    "project_memory_notes",
    "attachment_approvals",
    "execution_approval_decisions",
    "permission_requests",
    "attachments",
    "tasks",
    "executions",
    "messages",
    "conversation_reads",
    "invitations",
    "project_workspaces",
    "account_project_visits",
    "account_events",
    "agents",
    "project_users",
)


class ProjectLifecycle:
    def __init__(self, store):
        self.store = store

    def command(self, user, project, action, confirmation=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.store.require_member(db, project, user)
            if row["owner_id"] != user:
                raise PermissionError("只有群主可以管理项目归档")
            archived = row["archived_at"] is not None
            if action in {"archive", "restore"}:
                archive = action == "archive"
                if archived != archive:
                    row["archived_at"] = time.time() if archive else None
                    db.execute("UPDATE projects SET archived_at=? WHERE id=?", (row["archived_at"], project))
                    self.store.emit_project_event(db, project, "project")
                    self.store.log(db, user, "project_" + action, {"project": project})
                return {"archived_at": row["archived_at"]}
            if action not in {"delete-preview", "delete"}:
                raise ValueError("未知项目管理操作")
            if not archived:
                raise ValueError("请先归档项目")
            preview = self._preview(db, project, row["name"])
            if action == "delete-preview":
                return preview
            if confirmation != row["name"]:
                raise ValueError("请输入项目全名以确认删除")
            if preview["busy"]:
                raise ValueError("项目仍有执行或设备配置在处理中，请先等待完成或取消")
            self._delete(db, project, user)
            return {"deleted": True}

    @staticmethod
    def _preview(db, project, name):
        tables = {
            "messages": ("messages",),
            "tasks": ("project_work_drafts", "project_work_items"),
            "runs": ("tasks", "executions"),
            "memory": ("project_memory_notes",),
            "attachments": ("attachments",),
        }
        counts = {
            kind: sum(
                db.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id=?", (project,)).fetchone()[0]
                for table in sources
            )
            for kind, sources in tables.items()
        }
        busy = db.execute(
            "SELECT 1 FROM tasks t WHERE t.project_id=? AND (t.state IN ('starting','unknown') OR "
            "(t.state='running' AND NOT EXISTS (SELECT 1 FROM pull_jobs j WHERE j.id=t.job AND j.state='queued'))) "
            "UNION ALL SELECT 1 FROM executions e WHERE e.project_id=? AND (e.state IN ('starting','unknown') OR "
            "(e.state='running' AND NOT EXISTS (SELECT 1 FROM pull_jobs j WHERE j.id=e.id AND j.state='queued'))) "
            "UNION ALL SELECT 1 FROM pull_jobs j JOIN agents a ON a.id=j.agent_id WHERE a.project_id=? "
            "AND j.state NOT IN ('queued','needs_review','completed','failed','cancelled','timed_out','interrupted') "
            "UNION ALL SELECT 1 FROM owner_commands WHERE state='claimed' "
            "AND json_extract(envelope,'$.body.data.project_id')=? LIMIT 1",
            (project, project, project, project),
        ).fetchone()
        return {"name": name, "counts": counts, "busy": bool(busy)}

    def _delete(self, db, project, user):
        accounts = [r[0] for r in db.execute("SELECT user_id FROM project_users WHERE project_id=?", (project,))]
        for table in ("pull_jobs", "pull_bindings", "agent_cursors"):
            db.execute(f"DELETE FROM {table} WHERE agent_id IN (SELECT id FROM agents WHERE project_id=?)", (project,))
        db.execute("DELETE FROM owner_commands WHERE json_extract(envelope,'$.body.data.project_id')=?", (project,))
        for table in PROJECT_TABLES:
            db.execute(f"DELETE FROM {table} WHERE project_id=?", (project,))
        db.execute("DELETE FROM projects WHERE id=?", (project,))
        self.store.emit_account_event(db, accounts, "project")
        self.store.log(db, user, "project_delete", {"project": project})
