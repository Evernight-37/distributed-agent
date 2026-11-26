"""Bind remotely decided execution approvals to a receiver's local snapshot."""

import hashlib
import json
import re
import time
from pathlib import Path


def execution_digest(project, template_name, template):
    """Commit to the registered command and the local inputs that govern its execution."""
    snapshot = {
        "project": project["name"],
        "template_name": template_name,
        "template": template,
        "workspace": str(Path(project["remote_path"]).resolve()),
        "timeout": project.get("timeout", 1800),
    }
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def valid_execution_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


_OWNED_EXECUTIONS = """
SELECT j.id,j.node,j.agent_id,j.state AS job_state,j.result,j.cancel_requested,j.created_at,
       e.project_id,e.template,e.state AS execution_state,e.cancel_requested AS execution_cancelled,
       a.name AS agent_name,p.name AS project_name,a.enabled,a.configuring,p.paused,p.archived_at,
       b.templates,d.job_id AS decided,d.digest AS decided_digest,d.approved AS decision
FROM pull_jobs j
JOIN executions e ON e.id=j.id AND e.agent_id=j.agent_id
JOIN agents a ON a.id=j.agent_id AND a.project_id=e.project_id AND a.node=j.node
JOIN nodes n ON n.name=j.node AND n.owner_id=a.owner_id
JOIN projects p ON p.id=e.project_id
JOIN project_users u ON u.project_id=p.id AND u.user_id=a.owner_id
LEFT JOIN pull_bindings b ON b.agent_id=a.id
LEFT JOIN execution_approval_decisions d ON d.job_id=j.id AND d.owner_id=a.owner_id AND d.project_id=p.id
WHERE a.owner_id=? AND j.kind='execution' AND json_extract(j.request,'$.template')=e.template
"""


def _waiting(row):
    return (
        row["job_state"] == "running"
        and not row["cancel_requested"]
        and row["execution_state"] in {"awaiting_owner", "approved", "starting", "running", "unknown"}
        and not row["execution_cancelled"]
        and row["enabled"]
        and not row["configuring"]
        and not row["paused"]
        and row["template"] in json.loads(row["templates"] or "[]")
        and json.loads(row["result"] or "{}").get("local_status") == "waiting_local_approval"
    )


def _reported_digest(row):
    value = json.loads(row["result"] or "{}").get("execution_approval_digest")
    return value if valid_execution_digest(value) else None


def execution_requests(db, user, project=None):
    query = _OWNED_EXECUTIONS + " AND j.state='running' AND d.job_id IS NULL"
    args = [user]
    if project is not None:
        query += " AND e.project_id=?"
        args.append(project)
    query += " ORDER BY j.created_at,j.id"
    requests = []
    keys = ("id", "node", "agent_id", "agent_name", "project_id", "project_name", "template", "created_at")
    for row in db.execute(query, args):
        if _waiting(row):
            snapshot = _reported_digest(row)
            requests.append(
                dict(
                    ((key, row[key]) for key in keys),
                    kind="execution",
                    state="pending",
                    digest=snapshot,
                    can_decide=snapshot is not None,
                )
            )
    return requests


def decide_execution(db, user, project, request_id, approve, expected_digest):
    if type(approve) is not bool or not valid_execution_digest(expected_digest):
        raise ValueError("批准登记命令需要有效的命令快照摘要")
    row = db.execute(_OWNED_EXECUTIONS + " AND e.project_id=? AND j.id=?", (user, project, request_id)).fetchone()
    if not row:
        raise PermissionError("登记命令申请不存在或不属于当前账户")
    if row["decided"]:
        if row["decided_digest"] != expected_digest or bool(row["decision"]) != approve:
            raise PermissionError("这项登记命令申请已经处理")
    else:
        if approve and row["archived_at"] is not None:
            raise PermissionError("项目已归档，恢复项目前不能启动登记命令")
        if not _waiting(row) or _reported_digest(row) != expected_digest:
            raise PermissionError("登记命令尚未确认、已结束或快照已变化")
        db.execute(
            "INSERT INTO execution_approval_decisions(job_id,owner_id,project_id,digest,approved,decided_at)"
            " VALUES(?,?,?,?,?,?)",
            (request_id, user, project, expected_digest, approve, time.time()),
        )
    return {
        "id": request_id,
        "kind": "execution",
        "project_id": project,
        "digest": expected_digest,
        "state": "approved" if approve else "denied",
    }


def execution_decisions(db, node, owner):
    rows = db.execute(_OWNED_EXECUTIONS + " AND j.node=? AND d.job_id IS NOT NULL", (owner, node))
    return [
        {"job_id": row["id"], "digest": row["decided_digest"], "approved": bool(row["decision"])}
        for row in rows
        if _waiting(row)
        and (row["archived_at"] is None or not row["decision"])
        and row["decided_digest"] == _reported_digest(row)
    ]
