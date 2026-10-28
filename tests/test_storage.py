import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from agent_harness.collaboration_store import Store
from agent_harness.storage import (
    BACKUP_RETENTION,
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_VERSION,
    create_database_backup,
    inspect_database,
    migrate_database,
    restore_database,
)
from tests.helpers import provision_account


class StorageMigrationTests(unittest.TestCase):
    def test_online_backup_and_offline_restore_preserve_service_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "live")
            owner = provision_account(store, "owner", bootstrap=True)
            project = store.project_create(owner["id"], "before restore")
            backup = root / "exports" / "team.sqlite3"
            metadata = create_database_backup(store.database, backup)
            self.assertEqual(metadata["projects"], 1)
            self.assertEqual(metadata["owner_account_id"], owner["id"])
            store.project_create(owner["id"], "after backup")

            result = restore_database(store.database, backup, root / "live" / "backups")
            self.assertTrue(Path(result["safety_backup"]).is_file())
            restored = Store(root / "live")
            with restored.connect() as db:
                names = [row[0] for row in db.execute("SELECT name FROM projects")]
            self.assertEqual(names, [project["name"]])

    def test_invalid_or_future_restore_source_never_changes_live_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "live")
            owner = provision_account(store, "owner", bootstrap=True)
            store.project_create(owner["id"], "keep")
            corrupt = root / "corrupt.sqlite3"
            corrupt.write_bytes(b"not sqlite")
            with self.assertRaises(ValueError):
                restore_database(store.database, corrupt)

            future = root / "future.sqlite3"
            create_database_backup(store.database, future)
            with closing(sqlite3.connect(future)) as db:
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            with self.assertRaisesRegex(ValueError, "高于"):
                restore_database(store.database, future)
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT name FROM projects").fetchone()[0], "keep")

    def test_restore_migration_failure_rolls_back_original_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = Store(root / "live")
            owner = provision_account(live, "owner", bootstrap=True)
            live.project_create(owner["id"], "original")
            source_store = Store(root / "source")
            source_owner = provision_account(source_store, "source", bootstrap=True)
            source_store.project_create(source_owner["id"], "replacement")
            source = root / "replacement.sqlite3"
            create_database_backup(source_store.database, source)

            with patch("agent_harness.storage.migrate_database", side_effect=RuntimeError("migration failed")):
                with self.assertRaisesRegex(RuntimeError, "migration failed"):
                    restore_database(live.database, source, root / "live" / "backups")
            with live.connect() as db:
                self.assertEqual([row[0] for row in db.execute("SELECT name FROM projects")], ["original"])
                self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_restore_upgrades_v1_backup_and_preserves_service_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "v1.sqlite3"
            with closing(sqlite3.connect(source)) as db:
                db.executescript(SCHEMA_V1)
                db.execute("INSERT INTO service_metadata VALUES(1,'service-v1')")
                db.execute("INSERT INTO service_host(id,owner_account_id) VALUES(1,'owner-v1')")
                db.execute("PRAGMA user_version=1")
                db.commit()
            destination = root / "live" / "collaboration.sqlite3"
            restored = restore_database(destination, source)
            self.assertEqual(restored["source"]["schema_version"], 1)
            with closing(sqlite3.connect(destination)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(db.execute("SELECT service_id FROM service_metadata").fetchone()[0], "service-v1")
                self.assertEqual(
                    db.execute("SELECT owner_account_id FROM service_host").fetchone()[0],
                    "owner-v1",
                )

    def test_inspection_rejects_unrelated_sqlite_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "other.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE unrelated(value TEXT)")
            with self.assertRaisesRegex(ValueError, "不是 Agent Harness"):
                inspect_database(path)

    def test_housekeeping_removes_only_expired_ephemeral_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            now = time.time()
            with store.connect() as db:
                db.execute("INSERT INTO account_challenges VALUES('old','key',?)", (now - 1,))
                db.execute(
                    "INSERT INTO account_challenges VALUES('live','key',?)",
                    (now + 100,),
                )
            store.housekeep(now)
            with store.connect() as db:
                self.assertEqual(
                    [row[0] for row in db.execute("SELECT nonce FROM account_challenges")],
                    ["live"],
                )

    def test_fresh_database_has_versioned_complete_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            with store.connect() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue(
                    {
                        "users",
                        "messages",
                        "account_devices",
                        "pull_jobs",
                        "employees",
                        "project_memory_notes",
                        "account_event_sequences",
                        "account_events",
                        "file_transfers",
                        "file_transfer_tokens",
                        "project_workspaces",
                        "schema_migrations",
                    }.issubset(tables)
                )
                self.assertNotIn("sessions", tables)
                self.assertNotIn("project_artifacts", tables)
                self.assertNotIn("artifact_tokens", tables)
                self.assertEqual(
                    [row[1] for row in db.execute("PRAGMA table_info(users)")],
                    ["id", "username"],
                )
                self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertFalse(list((Path(temporary) / "backups").glob("*.sqlite3")))

    def test_v1_database_is_backed_up_then_migrated_to_event_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "collaboration.sqlite3"
            with closing(sqlite3.connect(database)) as db:
                db.executescript(SCHEMA_V1)
                db.execute("INSERT INTO service_metadata VALUES(1,'service-test')")
                db.execute("INSERT INTO service_host(id,owner_account_id) VALUES(1,'owner')")
                db.execute("PRAGMA user_version=1")
                db.commit()
            result = migrate_database(database, root / "backups")
            self.assertEqual(result["from"], 1)
            self.assertEqual(result["to"], SCHEMA_VERSION)
            self.assertTrue(Path(result["backup"]).is_file())
            with closing(sqlite3.connect(result["backup"])) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
            with closing(sqlite3.connect(database)) as migrated:
                tables = {row[0] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("account_events", tables)

    def test_v2_password_schema_migrates_to_identity_only_users(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "collaboration.sqlite3"
            with closing(sqlite3.connect(database)) as db:
                db.executescript(SCHEMA_V1)
                db.executescript(SCHEMA_V2)
                db.execute("INSERT INTO users VALUES('account-1','alice','salt','hash')")
                db.execute("INSERT INTO projects(id,name,owner_id) VALUES('p','project','account-1')")
                db.execute("INSERT INTO project_users VALUES('p','account-1')")
                db.execute("INSERT INTO sessions VALUES('session','account-1','csrf',9999999999)")
                db.execute("INSERT INTO service_metadata VALUES(1,'service-test')")
                db.execute("INSERT INTO service_host VALUES(1,'account-1',NULL)")
                db.execute("PRAGMA user_version=2")
                db.commit()
            result = migrate_database(database, root / "backups")
            self.assertEqual(result["from"], 2)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(
                    [row[1] for row in db.execute("PRAGMA table_info(users)")],
                    ["id", "username"],
                )
                self.assertEqual(db.execute("SELECT * FROM users").fetchone(), ("account-1", "alice"))
                self.assertEqual(db.execute("SELECT name FROM projects").fetchone()[0], "project")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn("sessions", tables)
                self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_v2_to_v3_failure_rolls_back_password_table_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "collaboration.sqlite3"
            with closing(sqlite3.connect(database)) as db:
                db.executescript(SCHEMA_V1)
                db.executescript(SCHEMA_V2)
                db.execute("INSERT INTO users VALUES('account-1','alice','salt','hash')")
                db.execute("DROP TABLE sessions")
                db.execute("CREATE VIEW sessions AS SELECT id FROM users")
                db.execute("PRAGMA user_version=2")
                db.commit()
            with self.assertRaises(sqlite3.OperationalError):
                migrate_database(database, root / "backups")
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(
                    [row[1] for row in db.execute("PRAGMA table_info(users)")],
                    ["id", "username", "salt", "password_hash"],
                )
                self.assertEqual(db.execute("SELECT username FROM users").fetchone()[0], "alice")
                self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertTrue(list((root / "backups").glob(f"collaboration-v2-to-v{SCHEMA_VERSION}-*.sqlite3")))

    def test_legacy_database_is_backed_up_and_preserved_before_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "collaboration.sqlite3"
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL)"
                )
                db.execute("INSERT INTO users VALUES('user-1','alice','00','11')")
                db.commit()
            result = migrate_database(database, root / "backups")
            self.assertTrue(result["migrated"])
            backup = Path(result["backup"])
            self.assertTrue(backup.is_file())
            with closing(sqlite3.connect(backup)) as db:
                self.assertEqual(db.execute("SELECT username FROM users").fetchone()[0], "alice")
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT username FROM users").fetchone()[0], "alice")
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_newer_database_fails_closed_without_modification(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "collaboration.sqlite3"
            with closing(sqlite3.connect(database)) as db:
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            with self.assertRaisesRegex(RuntimeError, "高于当前程序支持"):
                migrate_database(database)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 1)

    def test_unreleased_project_artifact_v4_is_backed_up_and_rewritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Store(root).database
            with closing(sqlite3.connect(database)) as db:
                db.executescript(
                    """
                    DROP TABLE file_transfer_tokens;
                    DROP TABLE file_transfers;
                    DROP TABLE project_workspaces;
                    CREATE TABLE project_artifacts(id TEXT PRIMARY KEY);
                    CREATE TABLE artifact_tokens(token_hash TEXT PRIMARY KEY);
                    INSERT INTO project_artifacts VALUES('retired-local-build-row');
                    PRAGMA user_version=4;
                    """
                )
            result = migrate_database(database, root / "backups")
            self.assertTrue(result["migrated"])
            self.assertTrue(Path(result["backup"]).is_file())
            with closing(sqlite3.connect(database)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn("project_artifacts", tables)
                self.assertNotIn("artifact_tokens", tables)
                self.assertIn("retired_project_artifacts_v21", tables)
                self.assertEqual(
                    db.execute("SELECT id FROM retired_project_artifacts_v21").fetchone()[0],
                    "retired-local-build-row",
                )
                self.assertTrue({"file_transfers", "file_transfer_tokens", "project_workspaces"}.issubset(tables))

    def test_backup_retention_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(BACKUP_RETENTION + 3):
                path = root / "backups" / f"collaboration-v0-to-v1-{index}.sqlite3"
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"old")
            database = root / "collaboration.sqlite3"
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL)"
                )
            migrate_database(database, root / "backups")
            self.assertLessEqual(len(list((root / "backups").glob("*.sqlite3"))), BACKUP_RETENTION)


if __name__ == "__main__":
    unittest.main()
