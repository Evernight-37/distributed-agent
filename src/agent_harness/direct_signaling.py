"""Signaling for direct transfers: the service learns who sends what to whom,
never the bytes. Content goes from device to device over the tailnet.
"""

import hashlib
import re
import sqlite3
import time

from agent_harness.collaboration_store import uid

STATES = {"queued", "accepted", "rejected", "running", "done", "failed"}
ADDRESS = re.compile(r"^[A-Za-z0-9.:\[\]_\-]{1,255}$")
TRANSFER_ID = re.compile(r"^transfer-[0-9a-f]{32}$")
NAME_LIMIT = 255
ONLINE_WINDOW = 45


def _online(db, node, now):
    if not node:
        return False
    row = db.execute("SELECT last_seen FROM pull_devices WHERE node=?", (node,)).fetchone()
    return bool(row and row["last_seen"] > now - ONLINE_WINDOW)


def peers(store, account):
    """Own devices and the devices of everyone sharing a project with us."""
    now = time.time()
    with store.connect() as db:
        contacts = []
        for contact in db.execute(
            "SELECT DISTINCT u.id,u.username FROM users u JOIN project_users theirs ON theirs.user_id=u.id "
            "JOIN project_users mine ON mine.project_id=theirs.project_id "
            "WHERE mine.user_id=? AND u.id<>? ORDER BY u.username,u.id",
            (account, account),
        ):
            devices = []
            for device in db.execute(
                "SELECT public_key,name FROM account_devices WHERE account_id=? AND revoked=0 ORDER BY name,public_key",
                (contact["id"],),
            ):
                node = "device-" + hashlib.sha256(device["public_key"].encode()).hexdigest()[:24]
                owned = db.execute("SELECT owner_id FROM nodes WHERE name=?", (node,)).fetchone()
                if owned and owned["owner_id"] == contact["id"]:
                    devices.append({"node": node, "name": device["name"], "online": _online(db, node, now)})
            contacts.append({"id": contact["id"], "username": contact["username"], "devices": devices})
        named = {
            "device-" + hashlib.sha256(row["public_key"].encode()).hexdigest()[:24]: row["name"]
            for row in db.execute(
                "SELECT public_key,name FROM account_devices WHERE account_id=? AND revoked=0", (account,)
            )
        }
        own = [
            {"node": row["name"], "name": named[row["name"]], "online": _online(db, row["name"], now)}
            for row in db.execute("SELECT name FROM nodes WHERE owner_id=? ORDER BY name", (account,))
            if row["name"] in named
        ]
        return {"contacts": contacts, "devices": own}


class Transfers:
    def __init__(self, store):
        self.store = store

    def _node(self, db, owner, node):
        row = db.execute("SELECT owner_id FROM nodes WHERE name=?", (node,)).fetchone()
        if not row or row["owner_id"] != owner:
            raise PermissionError("只能在自己名下的机器之间传输")
        return node

    def _owner_of(self, db, node):
        row = db.execute("SELECT owner_id FROM nodes WHERE name=?", (node,)).fetchone()
        if not row:
            raise ValueError("未知设备")
        return row["owner_id"]

    def offer(self, owner, from_node, to_node, name, size, address, recipient=None, transfer_id=None):
        if not isinstance(name, str) or not 1 <= len(name) <= NAME_LIMIT or "\0" in name:
            raise ValueError("文件名无效")
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 1024**4:
            raise ValueError("文件大小无效")
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise ValueError("传输地址无效")
        # The sender picks the id because it has already built its URL and its
        # signature around it; a second id minted here could never match.
        if transfer_id is not None and (not isinstance(transfer_id, str) or not TRANSFER_ID.fullmatch(transfer_id)):
            raise ValueError("传输编号无效")
        if from_node == to_node:
            raise ValueError("不能发给自己")
        if recipient is not None and to_node is not None:
            raise ValueError("请只指定接收账户或接收设备之一")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._owner_of(db, from_node) != owner:
                raise PermissionError("发送设备不属于当前账户")
            # A sender who knows the target device names it; a sender reaching a
            # teammate only names the account and lets them pick the machine.
            recipient = recipient or self._owner_of(db, to_node)
            same = recipient == owner
            if same and to_node is None:
                raise ValueError("同一账户下请指定接收设备")
            # Any account on this collaboration service may send; the recipient
            # decides. Sharing a project is not a precondition.
            if not same and not db.execute("SELECT 1 FROM users WHERE id=?", (recipient,)).fetchone():
                raise PermissionError("接收账户不在这个协作服务上")
            transfer = transfer_id or uid("transfer")
            now = time.time()
            # Inside one account the owner already consented by starting the
            # transfer; across accounts the recipient has to accept it.
            state = "accepted" if same else "queued"
            try:
                db.execute(
                    "INSERT INTO direct_transfers(id,from_node,to_node,owner_id,recipient_id,name,size,address,state,transferred,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,0,?,?)",
                    (transfer, from_node, to_node, owner, recipient, name, size, address, state, now, now),
                )
            except sqlite3.IntegrityError:
                raise ValueError("传输编号已被占用") from None
            return {"id": transfer, "state": state, "size": size, "name": name}

    def respond(self, owner, transfer, accept, node=None, claim_key=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.respond_db(db, owner, transfer, accept, node, claim_key)

    def respond_db(self, db, owner, transfer, accept, node=None, claim_key=None):
        """Same as respond, for a caller that already holds the write transaction."""
        row = db.execute("SELECT * FROM direct_transfers WHERE id=?", (transfer,)).fetchone()
        if not row or row["recipient_id"] != owner:
            raise PermissionError("只有接收方可以接受或拒绝")
        if row["state"] != "queued":
            raise ValueError("传输已被处理")
        state = "accepted" if accept else "rejected"
        update_node = row["to_node"]
        if accept:
            if update_node is None:
                if not node:
                    raise ValueError("请指定接收设备")
                self._node(db, owner, node)
                update_node = node
            elif node and node != update_node:
                raise ValueError("这条传输指定了另一台设备")
        # The sender has to know which key will sign the download; a teammate's
        # device only becomes known when it claims the offer.
        if claim_key is not None and (not isinstance(claim_key, str) or not 1 <= len(claim_key) <= 128):
            raise ValueError("无效的接收设备公钥")
        db.execute(
            "UPDATE direct_transfers SET state=?,to_node=?,claim_key=?,updated_at=? WHERE id=?",
            (state, update_node, claim_key or row["claim_key"], time.time(), transfer),
        )
        return {"id": transfer, "state": state, "to_node": update_node}

    def progress(self, owner, transfer, transferred, state=None, error=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.progress_db(db, owner, transfer, transferred, state, error)

    def progress_db(self, db, owner, transfer, transferred, state=None, error=None):
        """Same as progress, for a caller that already holds the write transaction."""
        if isinstance(transferred, bool) or not isinstance(transferred, int) or transferred < 0:
            raise ValueError("进度无效")
        if state is not None and state not in {"running", "done", "failed"}:
            raise ValueError("传输状态无效")
        row = db.execute("SELECT * FROM direct_transfers WHERE id=?", (transfer,)).fetchone()
        if not row or owner not in {row["owner_id"], row["recipient_id"]}:
            raise PermissionError("传输不存在")
        values = [transferred, time.time()]
        statement = "UPDATE direct_transfers SET transferred=?,updated_at=?"
        if state:
            statement += ",state=?"
            values.append(state)
        if error is not None:
            statement += ",error=?"
            values.append(str(error)[:500])
        statement += " WHERE id=?"
        values.append(transfer)
        db.execute(statement, tuple(values))
        return {"id": transfer, "state": state or row["state"]}

    def visible(self, db, node):
        """Offers waiting for this node, the ones it accepted, and its outgoing state."""
        rows = db.execute(
            "SELECT * FROM direct_transfers WHERE to_node=? AND state IN ('queued','accepted')"
            " ORDER BY created_at LIMIT 20",
            (node,),
        ).fetchall()
        # An account-addressed offer is visible to every device of that account
        # until one of them claims it by accepting.
        owner = db.execute("SELECT owner_id FROM nodes WHERE name=?", (node,)).fetchone()
        if owner:
            rows = (
                rows
                + db.execute(
                    "SELECT * FROM direct_transfers WHERE recipient_id=? AND to_node IS NULL AND state='queued'"
                    " ORDER BY created_at LIMIT 20",
                    (owner["owner_id"],),
                ).fetchall()
            )
        outgoing = db.execute(
            "SELECT * FROM direct_transfers WHERE from_node=? AND state IN ('accepted','rejected','done','failed','running')"
            " ORDER BY updated_at DESC LIMIT 20",
            (node,),
        ).fetchall()
        return [dict(row, transport="direct") for row in rows] + [dict(row, transport="direct") for row in outgoing]

    def view(self, owner):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM direct_transfers WHERE owner_id=? OR recipient_id=? ORDER BY created_at DESC LIMIT 50",
                (owner, owner),
            ).fetchall()
            return [dict(row, transport="direct") for row in rows]
