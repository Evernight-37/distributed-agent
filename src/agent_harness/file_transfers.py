"""Account/device file-transfer relay, independent from projects and Agents."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import shutil
import stat
import threading
import time
from contextlib import contextmanager

from agent_harness.attachments import safe_name

CHUNK_BYTES = 4 * 1024 * 1024
MAX_ACTIVE_UPLOADS_PER_ACCOUNT = 3
REQUEST_TTL = 7 * 86400
UPLOAD_TTL = 30 * 60
READY_TTL = 7 * 86400
TOKEN_TTL = 5 * 60
HISTORY_TTL = 30 * 86400
MIN_FREE_RESERVE = 256 * 1024 * 1024
_LOCK_GUARD = threading.Lock()
_LOCKS = {}
_RECOVERY_GUARD = threading.Lock()
_RECOVERED_ROOTS = set()


def _token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _regular_size(path, missing_ok=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("传输存储位置不是普通文件")
    return info.st_size


def _open_regular(path, writable=False):
    existing = _regular_size(path, missing_ok=True)
    if existing is None and not writable:
        raise FileNotFoundError(path)
    flags = os.O_RDWR if writable else os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | nofollow)
    except FileNotFoundError:
        if not writable:
            raise
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("传输存储位置不是普通文件")
        return os.fdopen(descriptor, "r+b" if writable else "rb")
    except Exception:
        os.close(descriptor)
        raise


def _sha256_file(path):
    digest = hashlib.sha256()
    with _open_regular(path) as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _transfer_lock(transfer_id):
    with _LOCK_GUARD:
        entry = _LOCKS.setdefault(transfer_id, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _LOCK_GUARD:
            entry[1] -= 1
            if not entry[1]:
                _LOCKS.pop(transfer_id, None)


class FileTransfers:
    """Durable control state plus temporary chunked bytes on a shared service."""

    def __init__(self, store):
        self.store = store
        self.root = (store.root / "file-transfers").resolve()
        self.staging = self.root / ".staging"
        self.trash = self.root / ".trash"
        for directory in (self.root, self.staging, self.trash):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        with _RECOVERY_GUARD:
            if self.root not in _RECOVERED_ROOTS:
                self.recover_interrupted_commits()
                _RECOVERED_ROOTS.add(self.root)

    def _part(self, transfer_id):
        return self.staging / f"{transfer_id}.part"

    def _file(self, transfer_id):
        return self.root / f"{transfer_id}.bin"

    def recover_interrupted_commits(self):
        """Reconcile the two filesystem/SQLite commit windows after a crash."""

        with _transfer_lock("__recovery__"):
            with self.store.connect() as db:
                rows = db.execute(
                    "SELECT * FROM file_transfers WHERE state IN ('uploading','ready','completed','cancelled','expired')"
                ).fetchall()
            for row in rows:
                transfer_id = row["id"]
                source = self._file(transfer_id)
                part = self._part(transfer_id)
                trashed = sorted(self.trash.glob(f"{transfer_id}.*"))
                if row["state"] == "uploading" and _regular_size(part, missing_ok=True) is None:
                    if _regular_size(source, missing_ok=True) == row["size"] and hmac.compare_digest(
                        _sha256_file(source), row["sha256"]
                    ):
                        now = time.time()
                        with self.store.connect() as db:
                            db.execute("BEGIN IMMEDIATE")
                            current = db.execute(
                                "SELECT state FROM file_transfers WHERE id=?", (transfer_id,)
                            ).fetchone()
                            if current and current["state"] == "uploading":
                                db.execute(
                                    "UPDATE file_transfers SET state='ready',received_bytes=size,updated_at=?,expires_at=? "
                                    "WHERE id=?",
                                    (now, now + READY_TTL, transfer_id),
                                )
                                db.execute("DELETE FROM file_transfer_tokens WHERE transfer_id=?", (transfer_id,))
                elif row["state"] == "ready" and _regular_size(source, missing_ok=True) is None:
                    candidate = next(
                        (
                            path
                            for path in trashed
                            if _regular_size(path, missing_ok=True) == row["size"]
                            and hmac.compare_digest(_sha256_file(path), row["sha256"])
                        ),
                        None,
                    )
                    if candidate is not None:
                        os.replace(candidate, source)
                elif row["state"] in {"completed", "cancelled", "expired"}:
                    for path in trashed:
                        path.unlink(missing_ok=True)

    @staticmethod
    def _sha(value):
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError("文件 SHA-256 无效")
        try:
            int(value, 16)
        except ValueError as exc:
            raise ValueError("文件 SHA-256 无效") from exc
        return value.lower()

    @staticmethod
    def _node_owned(db, account_id, node):
        if not isinstance(node, str) or not node:
            return False
        row = db.execute("SELECT owner_id FROM nodes WHERE name=?", (node,)).fetchone()
        return bool(row and row["owner_id"] == account_id)

    @staticmethod
    def _share_service_project(db, first, second):
        return bool(
            db.execute(
                "SELECT 1 FROM project_users a JOIN project_users b ON b.project_id=a.project_id "
                "WHERE a.user_id=? AND b.user_id=? LIMIT 1",
                (first, second),
            ).fetchone()
        )

    @staticmethod
    def _online(db, node, now):
        if not node:
            return False
        row = db.execute("SELECT last_seen FROM pull_devices WHERE node=?", (node,)).fetchone()
        return bool(row and row["last_seen"] > now - 45)

    def _public(self, db, row, now=None):
        now = time.time() if now is None else now
        return {
            "id": row["id"],
            "sender_id": row["sender_id"],
            "sender_node": row["sender_node"],
            "recipient_id": row["recipient_id"],
            "recipient_node": row["recipient_node"],
            "name": row["name"],
            "size": row["size"],
            "sha256": row["sha256"],
            "state": row["state"],
            "received_bytes": row["received_bytes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "accepted_at": row["accepted_at"],
            "completed_at": row["completed_at"],
            "expires_at": row["expires_at"],
            "sender_online": self._online(db, row["sender_node"], now),
            "recipient_online": self._online(db, row["recipient_node"], now),
        }

    def recipients(self, account_id):
        """List same-account devices and users with a shared project."""
        with self.store.connect() as db:
            contact_rows = db.execute(
                "SELECT DISTINCT u.id,u.username FROM users u JOIN project_users theirs ON theirs.user_id=u.id "
                "JOIN project_users mine ON mine.project_id=theirs.project_id "
                "WHERE mine.user_id=? AND u.id<>? ORDER BY u.username,u.id",
                (account_id, account_id),
            ).fetchall()
            contacts = []
            now = time.time()
            for contact in contact_rows:
                devices = []
                for device in db.execute(
                    "SELECT public_key,name FROM account_devices WHERE account_id=? AND revoked=0 ORDER BY name,public_key",
                    (contact["id"],),
                ):
                    node = "device-" + hashlib.sha256(device["public_key"].encode()).hexdigest()[:24]
                    if self._node_owned(db, contact["id"], node):
                        devices.append({"node": node, "name": device["name"], "online": self._online(db, node, now)})
                contacts.append({"id": contact["id"], "username": contact["username"], "devices": devices})
            named = {}
            for row in db.execute(
                "SELECT public_key,name FROM account_devices WHERE account_id=? AND revoked=0",
                (account_id,),
            ):
                node = "device-" + hashlib.sha256(row["public_key"].encode()).hexdigest()[:24]
                named[node] = row["name"]
            own = [
                {"node": row["name"], "name": named[row["name"]], "online": self._online(db, row["name"], now)}
                for row in db.execute("SELECT name FROM nodes WHERE owner_id=? ORDER BY name", (account_id,))
                if row["name"] in named
            ]
            return {"contacts": contacts, "devices": own}

    def request(self, sender_id, sender_node, recipient_id, recipient_node, name, size, sha256):
        name = safe_name(name)
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError("文件大小必须大于 0")
        sha256 = self._sha(sha256)
        now = time.time()
        self.cleanup(now)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._node_owned(db, sender_id, sender_node):
                raise PermissionError("发送设备不属于当前账户")
            if not db.execute("SELECT 1 FROM users WHERE id=?", (recipient_id,)).fetchone():
                raise ValueError("接收账户未加入该协作服务")
            same = sender_id == recipient_id
            if not same and not self._share_service_project(db, sender_id, recipient_id):
                raise PermissionError("只能向与自己共同参与项目的成员发送文件")
            if same:
                if not recipient_node or recipient_node == sender_node:
                    raise ValueError("请选择同一账户下的另一台接收设备")
                if not self._node_owned(db, recipient_id, recipient_node):
                    raise PermissionError("接收设备不属于当前账户")
                state, accepted = "accepted", now
            else:
                if recipient_node and not self._node_owned(db, recipient_id, recipient_node):
                    raise PermissionError("接收设备不属于接收账户")
                state, accepted = "requested", None
            transfer_id = "transfer-" + secrets.token_hex(16)
            db.execute(
                "INSERT INTO file_transfers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    sender_id,
                    sender_node,
                    recipient_id,
                    recipient_node,
                    name,
                    size,
                    sha256,
                    state,
                    0,
                    now,
                    now,
                    accepted,
                    None,
                    now + REQUEST_TTL,
                ),
            )
            row = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
            return self._public(db, row, now)

    def list(self, account_id):
        self.cleanup()
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM file_transfers WHERE sender_id=? OR recipient_id=? ORDER BY updated_at DESC LIMIT 200",
                (account_id, account_id),
            ).fetchall()
            return [self._public(db, row) for row in rows]

    def list_for_recipient_node(self, account_id, node):
        """Return only transfers assigned to one authenticated receiving node."""

        self.cleanup()
        with self.store.connect() as db:
            if not self._node_owned(db, account_id, node):
                raise PermissionError("接收设备不属于当前账户")
            rows = db.execute(
                "SELECT * FROM file_transfers WHERE recipient_id=? AND recipient_node=? "
                "ORDER BY updated_at DESC LIMIT 200",
                (account_id, node),
            ).fetchall()
            return [self._public(db, row) for row in rows]

    def status(self, account_id, transfer_id):
        self.cleanup()
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
            if not row or account_id not in {row["sender_id"], row["recipient_id"]}:
                raise ValueError("文件传输不存在")
            return self._public(db, row)

    def decide(self, account_id, node, transfer_id, accept, recipient_node=None):
        if not isinstance(accept, bool):
            raise ValueError("接收决定必须是布尔值")
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
            if not row or row["recipient_id"] != account_id:
                raise PermissionError("无权处理此文件请求")
            if row["sender_id"] == row["recipient_id"]:
                raise ValueError("同账户设备传输不需要人工确认")
            target_node = recipient_node or node
            if not self._node_owned(db, account_id, target_node):
                raise PermissionError("接收设备不属于当前账户")
            if row["recipient_node"] and row["recipient_node"] != target_node:
                raise PermissionError("此请求已指定其他接收设备")
            if row["state"] in {"accepted", "uploading", "ready"} and accept:
                return self._public(db, row, now)
            if row["state"] == "rejected" and not accept:
                return self._public(db, row, now)
            if row["state"] != "requested":
                raise ValueError("文件请求状态已经改变")
            state = "accepted" if accept else "rejected"
            db.execute(
                "UPDATE file_transfers SET recipient_node=?,state=?,updated_at=?,accepted_at=?,completed_at=?,expires_at=? WHERE id=?",
                (
                    target_node,
                    state,
                    now,
                    now if accept else None,
                    None if accept else now,
                    now + (REQUEST_TTL if accept else HISTORY_TTL),
                    transfer_id,
                ),
            )
            row = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
            return self._public(db, row, now)

    def _disk_preflight(self, db, transfer):
        remaining = transfer["size"] - transfer["received_bytes"]
        reserved = db.execute(
            "SELECT COALESCE(SUM(size-received_bytes),0) FROM file_transfers WHERE state='uploading' AND id<>?",
            (transfer["id"],),
        ).fetchone()[0]
        disk = shutil.disk_usage(self.root)
        reserve = max(MIN_FREE_RESERVE, int(disk.total * 0.02))
        if remaining + reserved + reserve > disk.free:
            raise RuntimeError("协作服务磁盘空间不足，无法接收此文件")

    def upload_start(self, account_id, node, transfer_id):
        now = time.time()
        self.cleanup(now)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
            if not row or row["sender_id"] != account_id or row["sender_node"] != node:
                raise PermissionError("只有发起请求的设备可以上传")
            if row["state"] not in {"accepted", "uploading"}:
                raise ValueError("接收方尚未同意或传输已经结束")
            if row["state"] == "accepted":
                active = db.execute(
                    "SELECT COUNT(*) FROM file_transfers WHERE sender_id=? AND state='uploading'", (account_id,)
                ).fetchone()[0]
                if active >= MAX_ACTIVE_UPLOADS_PER_ACCOUNT:
                    raise RuntimeError("同时发送的文件过多，请先等待已有传输完成")
            part_size = _regular_size(self._part(transfer_id), missing_ok=True)
            received = row["received_bytes"]
            if part_size is None:
                received = 0
            elif part_size < received:
                self._part(transfer_id).unlink()
                received = 0
            elif part_size > row["size"]:
                self._part(transfer_id).unlink()
                received = 0
            elif part_size != received:
                received = min(part_size, row["size"])
            refreshed = dict(row)
            refreshed["received_bytes"] = received
            self._disk_preflight(db, refreshed)
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM file_transfer_tokens WHERE transfer_id=? AND purpose='upload'", (transfer_id,))
            db.execute(
                "INSERT INTO file_transfer_tokens VALUES(?,?,?,?,?,?)",
                (_token_hash(token), transfer_id, account_id, node, "upload", now + UPLOAD_TTL),
            )
            db.execute(
                "UPDATE file_transfers SET state='uploading',received_bytes=?,updated_at=?,expires_at=? WHERE id=?",
                (received, now, now + UPLOAD_TTL, transfer_id),
            )
        return {"id": transfer_id, "upload_token": token, "chunk_bytes": CHUNK_BYTES, "received_bytes": received}

    def _authorize_token(self, db, token, transfer_id, purpose):
        if not isinstance(token, str) or len(token) > 256:
            raise PermissionError("传输凭据无效")
        hashed = _token_hash(token)
        token_row = db.execute(
            "SELECT * FROM file_transfer_tokens WHERE token_hash=? AND transfer_id=? AND purpose=? AND expires_at>?",
            (hashed, transfer_id, purpose, time.time()),
        ).fetchone()
        if not token_row or not hmac.compare_digest(token_row["token_hash"], hashed):
            raise PermissionError("传输凭据无效或已过期")
        transfer = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
        if not transfer:
            raise PermissionError("文件传输不存在")
        return token_row, transfer

    def upload_chunk(self, token, transfer_id, start, total, content, chunk_sha256):
        with _transfer_lock(transfer_id):
            if not isinstance(content, bytes) or not content or len(content) > CHUNK_BYTES:
                raise ValueError("分块大小无效")
            chunk_sha256 = self._sha(chunk_sha256)
            if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), chunk_sha256):
                raise ValueError("分块校验失败")
            now = time.time()
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                token_row, transfer = self._authorize_token(db, token, transfer_id, "upload")
                if (
                    transfer["state"] != "uploading"
                    or token_row["account_id"] != transfer["sender_id"]
                    or token_row["node"] != transfer["sender_node"]
                ):
                    raise PermissionError("无权继续此上传")
                if total != transfer["size"] or start != transfer["received_bytes"]:
                    raise ValueError(f"上传位置不一致；应从 {transfer['received_bytes']} 字节继续")
                if start + len(content) > total:
                    raise ValueError("分块超出文件大小")
                with _open_regular(self._part(transfer_id), writable=True) as stream:
                    stream.truncate(start)
                    stream.seek(start)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                received = start + len(content)
                db.execute(
                    "UPDATE file_transfers SET received_bytes=?,updated_at=?,expires_at=? WHERE id=?",
                    (received, now, now + UPLOAD_TTL, transfer_id),
                )
                db.execute(
                    "UPDATE file_transfer_tokens SET expires_at=? WHERE token_hash=?",
                    (now + UPLOAD_TTL, token_row["token_hash"]),
                )
            return {"id": transfer_id, "received_bytes": received, "complete": received == total}

    def finish(self, account_id, node, transfer_id):
        with _transfer_lock(transfer_id):
            with self.store.connect() as db:
                transfer = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
                if not transfer or transfer["sender_id"] != account_id or transfer["sender_node"] != node:
                    raise PermissionError("只有发送设备可以完成上传")
                if transfer["state"] == "ready":
                    return self._public(db, transfer)
                if transfer["state"] != "uploading" or transfer["received_bytes"] != transfer["size"]:
                    raise ValueError("文件尚未上传完成")
            part = self._part(transfer_id)
            destination = self._file(transfer_id)
            if _regular_size(part, missing_ok=True) != transfer["size"]:
                raise ValueError("上传暂存文件不完整")
            if not hmac.compare_digest(_sha256_file(part), transfer["sha256"]):
                raise ValueError("完整文件 SHA-256 校验失败")
            if _regular_size(destination, missing_ok=True) is not None:
                raise RuntimeError("传输完成位置已经存在")
            os.replace(part, destination)
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
            now = time.time()
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
                if not current or current["state"] != "uploading" or current["sender_id"] != account_id:
                    os.replace(destination, part)
                    raise RuntimeError("传输状态已经改变")
                db.execute(
                    "UPDATE file_transfers SET state='ready',updated_at=?,expires_at=? WHERE id=?",
                    (now, now + READY_TTL, transfer_id),
                )
                db.execute("DELETE FROM file_transfer_tokens WHERE transfer_id=?", (transfer_id,))
                row = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
                return self._public(db, row, now)

    def download_ticket(self, account_id, node, transfer_id):
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
            if (
                not row
                or row["recipient_id"] != account_id
                or row["recipient_node"] != node
                or row["state"] != "ready"
                or _regular_size(self._file(transfer_id), missing_ok=True) != row["size"]
            ):
                raise PermissionError("此设备不能下载该文件")
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO file_transfer_tokens VALUES(?,?,?,?,?,?)",
                (_token_hash(token), transfer_id, account_id, node, "download", now + TOKEN_TTL),
            )
            return {"download_token": token, **self._public(db, row, now)}

    def download_chunk(self, token, transfer_id, start, end):
        with _transfer_lock(transfer_id):
            with self.store.connect() as db:
                token_row, transfer = self._authorize_token(db, token, transfer_id, "download")
                if (
                    transfer["state"] != "ready"
                    or token_row["account_id"] != transfer["recipient_id"]
                    or token_row["node"] != transfer["recipient_node"]
                ):
                    raise PermissionError("无权下载此文件")
                db.execute(
                    "UPDATE file_transfer_tokens SET expires_at=? WHERE token_hash=?",
                    (time.time() + TOKEN_TTL, token_row["token_hash"]),
                )
            size = transfer["size"]
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start or start >= size:
                raise ValueError("下载范围无效")
            end = min(end, size - 1)
            if end - start + 1 > CHUNK_BYTES:
                raise ValueError("单次下载范围过大")
            path = self._file(transfer_id)
            if _regular_size(path, missing_ok=True) != size:
                raise RuntimeError("传输文件存储缺失")
            with _open_regular(path) as stream:
                stream.seek(start)
                content = stream.read(end - start + 1)
            return transfer, start, end, content

    def complete(self, account_id, node, transfer_id):
        with _transfer_lock(transfer_id):
            with self.store.connect() as db:
                transfer = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
                if not transfer or transfer["recipient_id"] != account_id or transfer["recipient_node"] != node:
                    raise PermissionError("只有接收设备可以完成传输")
                if transfer["state"] == "completed":
                    return self._public(db, transfer)
                if transfer["state"] != "ready":
                    raise ValueError("文件尚未可下载")
            source = self._file(transfer_id)
            trashed = self.trash / f"{transfer_id}.{secrets.token_hex(8)}"
            if _regular_size(source, missing_ok=True) is not None:
                os.replace(source, trashed)
            now = time.time()
            try:
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
                    if not current or current["state"] != "ready":
                        raise RuntimeError("传输状态已经改变")
                    db.execute(
                        "UPDATE file_transfers SET state='completed',updated_at=?,completed_at=?,expires_at=? WHERE id=?",
                        (now, now, now + HISTORY_TTL, transfer_id),
                    )
                    db.execute("DELETE FROM file_transfer_tokens WHERE transfer_id=?", (transfer_id,))
                    row = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
            except Exception:
                if _regular_size(trashed, missing_ok=True) is not None:
                    os.replace(trashed, source)
                raise
            trashed.unlink(missing_ok=True)
            with self.store.connect() as db:
                return self._public(db, row, now)

    def cancel(self, account_id, transfer_id):
        with _transfer_lock(transfer_id):
            now = time.time()
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
                if not row or account_id not in {row["sender_id"], row["recipient_id"]}:
                    raise PermissionError("无权取消此传输")
                if row["state"] in {"completed", "rejected", "cancelled", "expired"}:
                    return self._public(db, row, now)
                db.execute(
                    "UPDATE file_transfers SET state='cancelled',updated_at=?,completed_at=?,expires_at=? WHERE id=?",
                    (now, now, now + HISTORY_TTL, transfer_id),
                )
                db.execute("DELETE FROM file_transfer_tokens WHERE transfer_id=?", (transfer_id,))
                row = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
            self._part(transfer_id).unlink(missing_ok=True)
            self._file(transfer_id).unlink(missing_ok=True)
            with self.store.connect() as db:
                return self._public(db, row, now)

    def cleanup(self, now=None):
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM file_transfer_tokens WHERE expires_at<?", (now,))
            candidates = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM file_transfers WHERE expires_at<?",
                    (now,),
                )
            ]
        for transfer_id in candidates:
            remove_bytes = False
            with _transfer_lock(transfer_id):
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute("SELECT * FROM file_transfers WHERE id=?", (transfer_id,)).fetchone()
                    if not row or row["expires_at"] is None or row["expires_at"] >= now:
                        continue
                    if row["state"] in {"requested", "accepted", "uploading", "ready"}:
                        db.execute(
                            "UPDATE file_transfers SET state='expired',updated_at=?,completed_at=?,expires_at=? WHERE id=?",
                            (now, now, now + HISTORY_TTL, transfer_id),
                        )
                        db.execute("DELETE FROM file_transfer_tokens WHERE transfer_id=?", (transfer_id,))
                        remove_bytes = True
                    elif row["state"] in {"completed", "rejected", "cancelled", "expired"}:
                        db.execute("DELETE FROM file_transfer_tokens WHERE transfer_id=?", (transfer_id,))
                        db.execute("DELETE FROM file_transfers WHERE id=?", (transfer_id,))
                        remove_bytes = True
                if remove_bytes:
                    self._part(transfer_id).unlink(missing_ok=True)
                    self._file(transfer_id).unlink(missing_ok=True)
