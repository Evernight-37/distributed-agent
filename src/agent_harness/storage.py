"""Versioned SQLite bootstrap, backup and integrity checks for a project Hub."""

from __future__ import annotations

import os
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from agent_harness.common import lock


SCHEMA_VERSION = 4
BACKUP_RETENTION = 5


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id TEXT,csrf TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,owner_id TEXT NOT NULL,admin_agent_id TEXT,paused INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS project_users(project_id TEXT,user_id TEXT,PRIMARY KEY(project_id,user_id));
CREATE TABLE IF NOT EXISTS nodes(name TEXT PRIMARY KEY,owner_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,name TEXT NOT NULL,owner_id TEXT NOT NULL,node TEXT NOT NULL,backend_project TEXT NOT NULL,model TEXT NOT NULL,effort TEXT NOT NULL,timeout INTEGER NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,last_message INTEGER NOT NULL DEFAULT 0,configuring INTEGER NOT NULL DEFAULT 0,UNIQUE(node,backend_project));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT NOT NULL,sender_id TEXT NOT NULL,sender_type TEXT NOT NULL,recipient_id TEXT,body TEXT NOT NULL,created_at REAL NOT NULL,causal_task TEXT,mention_agent_id TEXT,conversation_id TEXT,client_message_id TEXT,sender_client TEXT);
CREATE TABLE IF NOT EXISTS conversation_reads(project_id TEXT NOT NULL,user_id TEXT NOT NULL,conversation_id TEXT NOT NULL,last_message INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(project_id,user_id,conversation_id));
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,agent_id TEXT NOT NULL,title TEXT NOT NULL,contract TEXT NOT NULL,state TEXT NOT NULL,job TEXT,report TEXT,created_at REAL NOT NULL,cutoff INTEGER NOT NULL DEFAULT 0,error TEXT,processed INTEGER NOT NULL DEFAULT 0,cancel_requested INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_task ON tasks(agent_id) WHERE state IN ('queued','starting','running','unknown');
CREATE TABLE IF NOT EXISTS executions(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,agent_id TEXT NOT NULL,template TEXT NOT NULL,state TEXT NOT NULL,result TEXT,created_at REAL NOT NULL,notified INTEGER NOT NULL DEFAULT 0,reply_to TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS invitations(hash TEXT PRIMARY KEY,project_id TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT,action TEXT,details TEXT,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_cursors(agent_id TEXT,channel TEXT,last_message INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(agent_id,channel));
CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,owner_id TEXT NOT NULL,name TEXT NOT NULL,mime TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,content BLOB NOT NULL,created_at REAL NOT NULL,message_id INTEGER REFERENCES messages(id));
CREATE INDEX IF NOT EXISTS attachments_message ON attachments(message_id);
CREATE TABLE IF NOT EXISTS service_host(id INTEGER PRIMARY KEY CHECK(id=1),owner_account_id TEXT NOT NULL,host_node TEXT);
CREATE TABLE IF NOT EXISTS service_metadata(id INTEGER PRIMARY KEY CHECK(id=1),service_id TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_human_message_submission ON messages(project_id,sender_id,client_message_id) WHERE client_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS account_devices(public_key TEXT PRIMARY KEY,account_id TEXT NOT NULL,name TEXT NOT NULL,chain TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS account_delegates(public_key TEXT PRIMARY KEY,account_id TEXT NOT NULL,name TEXT NOT NULL,scopes TEXT NOT NULL,chain TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS account_challenges(nonce TEXT PRIMARY KEY,public_key TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS account_requests(id TEXT PRIMARY KEY,code_hash TEXT UNIQUE NOT NULL,public_key TEXT NOT NULL,name TEXT NOT NULL,expires REAL NOT NULL,account_id TEXT,chain TEXT,claimed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS account_project_visits(account_id TEXT NOT NULL,project_id TEXT NOT NULL,visited REAL NOT NULL,PRIMARY KEY(account_id,project_id));
CREATE TABLE IF NOT EXISTS account_services(account_id TEXT NOT NULL,server TEXT NOT NULL,joined_at REAL NOT NULL,PRIMARY KEY(account_id,server));

CREATE TABLE IF NOT EXISTS pull_devices(node TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,last_seen REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS pull_pairs(hash TEXT PRIMARY KEY,node TEXT NOT NULL,owner_id TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pull_bindings(agent_id TEXT PRIMARY KEY,stages TEXT NOT NULL,templates TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pull_jobs(id TEXT PRIMARY KEY,node TEXT NOT NULL,agent_id TEXT NOT NULL,kind TEXT NOT NULL,request TEXT NOT NULL,state TEXT NOT NULL,lease_id TEXT NOT NULL,result TEXT,created_at REAL NOT NULL,cancel_requested INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS employees(id TEXT PRIMARY KEY,name TEXT NOT NULL,owner_id TEXT NOT NULL,node TEXT NOT NULL,settings TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS owner_commands(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,node TEXT NOT NULL,envelope TEXT NOT NULL,state TEXT NOT NULL,result TEXT,created_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS project_memory_notes(id INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT NOT NULL,author_id TEXT NOT NULL,body TEXT NOT NULL,created_at REAL NOT NULL,source_task_id TEXT);
CREATE INDEX IF NOT EXISTS project_memory_project ON project_memory_notes(project_id,id);

CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at REAL NOT NULL);
"""


SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS account_event_sequences(
    account_id TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL DEFAULT 0 CHECK(cursor >= 0)
);
CREATE TABLE IF NOT EXISTS account_events(
    account_id TEXT NOT NULL,
    cursor INTEGER NOT NULL CHECK(cursor > 0),
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(account_id,cursor)
);
CREATE INDEX IF NOT EXISTS account_events_project ON account_events(account_id,project_id,cursor);
"""


SCHEMA_V3 = """
ALTER TABLE users RENAME TO users_password_legacy;
CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL);
INSERT INTO users(id,username) SELECT id,username FROM users_password_legacy;
DROP TABLE users_password_legacy;
DROP TABLE IF EXISTS sessions;
"""


SCHEMA_V4 = """
DROP TABLE IF EXISTS artifact_tokens;
CREATE TABLE IF NOT EXISTS file_transfers(
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    sender_node TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    recipient_node TEXT,
    name TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size > 0),
    sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'requested','accepted','uploading','ready','completed',
        'rejected','cancelled','expired'
    )),
    received_bytes INTEGER NOT NULL DEFAULT 0 CHECK(received_bytes >= 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    accepted_at REAL,
    completed_at REAL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS file_transfers_sender
    ON file_transfers(sender_id,state,updated_at);
CREATE INDEX IF NOT EXISTS file_transfers_recipient
    ON file_transfers(recipient_id,state,updated_at);
CREATE TABLE IF NOT EXISTS file_transfer_tokens(
    token_hash TEXT PRIMARY KEY,
    transfer_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    node TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('upload','download')),
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS file_transfer_tokens_expiry ON file_transfer_tokens(expires_at);
CREATE TABLE IF NOT EXISTS project_workspaces(
    project_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    node TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ready','needs_choice','unavailable')),
    write_policy TEXT NOT NULL CHECK(write_policy IN ('single_writer','read_shared')),
    updated_at REAL NOT NULL,
    PRIMARY KEY(project_id,node)
);
"""


def _v4_layout_current(connection):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return (
        "file_transfers" in tables
        and "file_transfer_tokens" in tables
        and "project_workspaces" in tables
        and "project_artifacts" not in tables
    )


def _private(path, directory=False):
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        # Windows data-root ACLs are applied by the desktop bootstrap.
        pass


def _open(path):
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _backup(database, backup_root, source_version, target_version):
    backup_root.mkdir(parents=True, exist_ok=True)
    _private(backup_root, directory=True)
    destination = backup_root / (f"collaboration-v{source_version}-to-v{target_version}-{time.time_ns()}.sqlite3")
    with (
        closing(_open(database)) as source,
        closing(sqlite3.connect(destination)) as target,
    ):
        source.backup(target)
        target.commit()
    _private(destination)
    backups = sorted(
        backup_root.glob("collaboration-v*-to-v*-*.sqlite3"),
        key=lambda item: item.stat().st_mtime,
    )
    for stale in backups[:-BACKUP_RETENTION]:
        stale.unlink(missing_ok=True)
    return destination


def _integrity(connection):
    row = connection.execute("PRAGMA quick_check").fetchone()
    if not row or row[0] != "ok":
        raise RuntimeError("协作数据库完整性检查失败")


def inspect_database(database):
    """Validate a SQLite backup and return only non-sensitive metadata."""

    database = Path(database).expanduser().resolve()
    if not database.is_file() or database.is_symlink():
        raise ValueError("备份文件不存在或不是普通文件")
    digest = hashlib.sha256()
    with database.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)) as connection:
            _integrity(connection)
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise ValueError(f"备份数据库版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "service_metadata" not in tables or "service_host" not in tables:
                raise ValueError("所选文件不是 Agent Harness 协作服务备份")
            service = connection.execute("SELECT service_id FROM service_metadata WHERE id=1").fetchone()
            host = connection.execute("SELECT owner_account_id FROM service_host WHERE id=1").fetchone()
            if not service or not host:
                raise ValueError("备份缺少协作服务身份或所有者信息")
            counts = {}
            for table in ("projects", "users"):
                counts[table] = (
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if table in tables else 0
                )
    except sqlite3.Error as exc:
        raise ValueError("备份不是有效的 SQLite 数据库") from exc
    return {
        "path": str(database),
        "size": database.stat().st_size,
        "sha256": digest.hexdigest(),
        "schema_version": version,
        "service_id": service[0],
        "owner_account_id": host[0],
        "projects": counts["projects"],
        "users": counts["users"],
    }


def _copy_database(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with (
            closing(_open(source)) as source_connection,
            closing(sqlite3.connect(temporary)) as target,
        ):
            source_connection.backup(target)
            target.commit()
            _integrity(target)
        _private(temporary)
        os.replace(temporary, destination)
        _private(destination)
    finally:
        temporary.unlink(missing_ok=True)


def create_database_backup(database, destination):
    """Create an atomic, consistent online backup selected by the local owner."""

    database = Path(database).resolve()
    destination = Path(destination).expanduser().resolve()
    if destination == database:
        raise ValueError("备份文件不能覆盖正在使用的协作数据库")
    if destination.exists() and (destination.is_dir() or destination.is_symlink()):
        raise ValueError("备份目标必须是普通文件")
    _copy_database(database, destination)
    return inspect_database(destination)


def restore_database(database, source, backup_root=None):
    """Restore an offline Hub database with automatic pre-restore rollback."""

    database = Path(database).resolve()
    source = Path(source).expanduser().resolve()
    if source == database:
        raise ValueError("不能从正在使用的数据库自身恢复")
    source_info = inspect_database(source)
    backup_root = Path(backup_root or database.parent / "backups").resolve()
    with lock(database.with_suffix(database.suffix + ".restore.lock")):
        safety = _backup(database, backup_root, SCHEMA_VERSION, SCHEMA_VERSION) if database.is_file() else None
        staged = database.with_name(database.name + "." + uuid.uuid4().hex + ".restore")
        try:
            _copy_database(source, staged)
            for suffix in ("-wal", "-shm"):
                Path(str(database) + suffix).unlink(missing_ok=True)
            os.replace(staged, database)
            _private(database)
            try:
                migration = migrate_database(database, backup_root)
            except Exception:
                if safety is not None:
                    _copy_database(safety, staged)
                    for suffix in ("-wal", "-shm"):
                        Path(str(database) + suffix).unlink(missing_ok=True)
                    os.replace(staged, database)
                    _private(database)
                raise
        finally:
            staged.unlink(missing_ok=True)
    return {
        "source": source_info,
        "safety_backup": str(safety) if safety else None,
        "migration": migration,
    }


def migrate_database(database, backup_root=None):
    """Bring a Hub database to the current schema and return migration metadata."""

    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    _private(database.parent, directory=True)
    backup_root = Path(backup_root or database.parent / "backups").resolve()
    existed = database.is_file() and database.stat().st_size > 0
    backup = None
    with lock(database.with_suffix(database.suffix + ".migration.lock")):
        with closing(_open(database)) as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"协作数据库版本 {current} 高于当前程序支持的 {SCHEMA_VERSION}；请升级应用")
            if current == SCHEMA_VERSION and _v4_layout_current(connection):
                _integrity(connection)
                _private(database)
                return {
                    "from": current,
                    "to": current,
                    "backup": None,
                    "migrated": False,
                }
        rewrite_v4 = current == SCHEMA_VERSION
        if existed and (current < SCHEMA_VERSION or rewrite_v4):
            backup = _backup(database, backup_root, current, SCHEMA_VERSION)
        with closing(_open(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if current < 1:
                    for statement in SCHEMA_V1.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "INSERT OR IGNORE INTO service_metadata(id,service_id) VALUES(1,?)",
                        ("service-" + uuid.uuid4().hex,),
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO schema_migrations(version,applied_at) VALUES(?,?)",
                        (1, time.time()),
                    )
                    connection.execute("PRAGMA user_version=1")
                if current < 2:
                    for statement in SCHEMA_V2.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "INSERT OR REPLACE INTO schema_migrations(version,applied_at) VALUES(?,?)",
                        (2, time.time()),
                    )
                    connection.execute("PRAGMA user_version=2")
                if current < 3:
                    for statement in SCHEMA_V3.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "INSERT OR REPLACE INTO schema_migrations(version,applied_at) VALUES(?,?)",
                        (3, time.time()),
                    )
                    connection.execute("PRAGMA user_version=3")
                if current < 4 or rewrite_v4:
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "project_artifacts" in tables:
                        # The short-lived, unreleased v2.1 artifact design is no
                        # longer used.  Preserve its metadata for manual recovery
                        # instead of destroying it during the schema rewrite.
                        retired = "retired_project_artifacts_v21"
                        suffix = 2
                        while retired in tables:
                            retired = f"retired_project_artifacts_v21_{suffix}"
                            suffix += 1
                        connection.execute(f'ALTER TABLE project_artifacts RENAME TO "{retired}"')
                    for statement in SCHEMA_V4.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    for row in connection.execute("SELECT id,settings FROM employees").fetchall():
                        try:
                            settings = json.loads(row["settings"])
                        except (TypeError, ValueError):
                            continue
                        if isinstance(settings, dict) and "remote_path" in settings:
                            settings.pop("remote_path", None)
                            connection.execute(
                                "UPDATE employees SET settings=? WHERE id=?",
                                (json.dumps(settings, ensure_ascii=False, sort_keys=True), row["id"]),
                            )
                    connection.execute(
                        "INSERT OR REPLACE INTO schema_migrations(version,applied_at) VALUES(?,?)",
                        (4, time.time()),
                    )
                    connection.execute("PRAGMA user_version=4")
                connection.commit()
                _integrity(connection)
                connection.execute("PRAGMA journal_mode=WAL")
            except Exception:
                connection.rollback()
                raise
    _private(database)
    return {
        "from": current,
        "to": SCHEMA_VERSION,
        "backup": str(backup) if backup else None,
        "migrated": current != SCHEMA_VERSION or rewrite_v4,
    }
