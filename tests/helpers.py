"""Test-only fixtures for provisioning signed-account records without Web auth."""

import time

from agent_harness.collaboration_store import digest, uid


def provision_account(store, username, *, bootstrap=False, invite=None):
    """Insert a test account and optional membership; never imported by production code."""

    user_id = uid("account")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        project = None
        if bootstrap:
            if db.execute("SELECT 1 FROM users").fetchone():
                raise PermissionError("Initial test account already exists")
        else:
            invitation = db.execute(
                "SELECT * FROM invitations WHERE hash=? AND used=0 AND expires>?",
                (digest(invite or ""), time.time()),
            ).fetchone()
            if not invitation:
                raise PermissionError("Valid test invite required")
            project = invitation["project_id"]
        db.execute("INSERT INTO users(id,username) VALUES(?,?)", (user_id, username))
        if bootstrap:
            db.execute(
                "INSERT OR IGNORE INTO service_host(id,owner_account_id) VALUES(1,?)",
                (user_id,),
            )
        if project:
            db.execute("INSERT INTO project_users VALUES(?,?)", (project, user_id))
            db.execute("UPDATE invitations SET used=1 WHERE hash=?", (digest(invite),))
            store.emit_project_event(db, project, "membership")
    return {"id": user_id, "username": username}
