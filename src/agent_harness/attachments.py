"""Bounded chat attachments, authorized by their owning conversation."""

import base64
import binascii
import hashlib
import re
import secrets
import time

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENTS = 5
PROJECT_QUOTA = 512 * 1024 * 1024
METADATA = "id,name,mime,size,sha256"


def safe_name(name):
    if not isinstance(name, str):
        raise ValueError("附件文件名无效")
    name = re.sub(r"[\\/:\x00-\x1f\x7f]", "_", name).strip()[:180]
    if not name or name in {".", ".."}:
        raise ValueError("附件文件名无效")
    return name


def decode_file(encoded):
    if not isinstance(encoded, str) or len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
        raise ValueError("每个附件最大 5 MB")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("附件内容编码无效") from None
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("每个附件最大 5 MB")
    return data


def image_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


class Attachments:
    def __init__(self, store):
        self.store = store

    def upload(self, user, project, name, encoded):
        name, data = safe_name(name), decode_file(encoded)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.require_member(db, project, user)
            db.execute(
                "DELETE FROM attachments WHERE message_id IS NULL AND created_at<?",
                (time.time() - 86400,),
            )
            used = db.execute(
                "SELECT COALESCE(SUM(size),0) FROM attachments WHERE project_id=?",
                (project,),
            ).fetchone()[0]
            if used + len(data) > PROJECT_QUOTA:
                raise ValueError("本项目附件已达到 512 MB 试用容量")
            aid = "file-" + secrets.token_hex(16)
            db.execute(
                "INSERT INTO attachments(id,project_id,owner_id,name,mime,size,sha256,content,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    project,
                    user,
                    name,
                    image_mime(data),
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                    data,
                    time.time(),
                ),
            )
            return dict(db.execute(f"SELECT {METADATA} FROM attachments WHERE id=?", (aid,)).fetchone())

    def ids(self, ids):
        if not isinstance(ids, list) or len(ids) > MAX_ATTACHMENTS or any(not isinstance(x, str) for x in ids):
            raise ValueError("每条消息最多 5 个附件")
        if len(set(ids)) != len(ids):
            raise ValueError("附件不能重复")
        return ids

    def bind(self, db, user, project, message, ids):
        for aid in self.ids(ids):
            row = db.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
            if not row or row["project_id"] != project or row["owner_id"] != user or row["message_id"] is not None:
                raise PermissionError("附件不存在、已发送或不属于本账户和项目")
            db.execute("UPDATE attachments SET message_id=? WHERE id=?", (message, aid))

    def metadata(self, db, message):
        return [
            dict(row)
            for row in db.execute(
                f"SELECT {METADATA} FROM attachments WHERE message_id=? ORDER BY rowid",
                (message,),
            )
        ]

    def download(self, user, project, aid):
        with self.store.connect() as db:
            self.store.require_member(db, project, user)
            row = db.execute("SELECT * FROM attachments WHERE id=? AND project_id=?", (aid, project)).fetchone()
            if not row:
                raise PermissionError("附件不存在或无权访问")
            if row["message_id"] is None:
                if row["owner_id"] != user:
                    raise PermissionError("附件尚未发送")
            else:
                message = db.execute(
                    "SELECT conversation_id FROM messages WHERE id=?",
                    (row["message_id"],),
                ).fetchone()
                if not message or not message["conversation_id"]:
                    raise PermissionError("附件会话不可用")
                self.store._conversation_access(db, project, user, message["conversation_id"])
            result = {k: row[k] for k in ("id", "name", "mime", "size", "sha256")}
            result["base64"] = base64.b64encode(row["content"]).decode("ascii")
            return result
