"""Signaling for direct transfers: the service learns who sends what to whom,
never the bytes. Content goes from device to device over the tailnet.
"""

import re
import time

from agent_harness.collaboration_store import uid

STATES = {"queued", "accepted", "rejected", "running", "done", "failed"}
ADDRESS = re.compile(r"^[A-Za-z0-9.:\[\]_\-]{1,255}$")
NAME_LIMIT = 255


class Transfers:
    def __init__(self, store):
        self.store = store

    def _node(self, db, owner, node):
        row = db.execute("SELECT owner_id FROM nodes WHERE name=?", (node,)).fetchone()
        if not row or row["owner_id"] != owner:
            raise PermissionError("只能在自己名下的机器之间传输")
        return node

    def offer(self, owner, from_node, to_node, name, size, address):
        if not isinstance(name, str) or not 1 <= len(name) <= NAME_LIMIT or "\0" in name:
            raise ValueError("文件名无效")
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 1024**4:
            raise ValueError("文件大小无效")
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise ValueError("传输地址无效")
        if from_node == to_node:
            raise ValueError("不能发给自己")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._node(db, owner, from_node)
            self._node(db, owner, to_node)
            transfer = uid("transfer")
            now = time.time()
            db.execute(
                "INSERT INTO direct_transfers(id,from_node,to_node,owner_id,name,size,address,state,transferred,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,'queued',0,?,?)",
                (transfer, from_node, to_node, owner, name, size, address, now, now),
            )
            return {"id": transfer, "state": "queued", "size": size, "name": name}

    def respond(self, owner, transfer, accept):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM direct_transfers WHERE id=?", (transfer,)).fetchone()
            if not row or row["owner_id"] != owner:
                raise PermissionError("传输不存在")
            if row["state"] != "queued":
                raise ValueError("传输已被处理")
            state = "accepted" if accept else "rejected"
            db.execute(
                "UPDATE direct_transfers SET state=?,updated_at=? WHERE id=?",
                (state, time.time(), transfer),
            )
            return {"id": transfer, "state": state}

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
        if not row or row["owner_id"] != owner:
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
        outgoing = db.execute(
            "SELECT * FROM direct_transfers WHERE from_node=? AND state IN ('accepted','rejected','done','failed','running')"
            " ORDER BY updated_at DESC LIMIT 20",
            (node,),
        ).fetchall()
        return [dict(row) for row in rows] + [dict(row) for row in outgoing]

    def view(self, owner):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM direct_transfers WHERE owner_id=? ORDER BY created_at DESC LIMIT 50",
                (owner,),
            ).fetchall()
            return [dict(row) for row in rows]
