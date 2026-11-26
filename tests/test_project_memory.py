import json
import tempfile
import unittest

from agent_harness.collaboration_store import Store
from agent_harness.coordinator import Coordinator
from agent_harness.project_memory import ProjectMemory
from agent_harness.storage import SCHEMA_V9, migrate_database
from tests.helpers import provision_account


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.owner = provision_account(self.store, "owner", bootstrap=True)["id"]
        self.project = self.store.project_create(self.owner, "Research")["id"]
        self.memory = ProjectMemory(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_project_notes_are_shared_and_owner_can_delete(self):
        invite = self.store.invite(self.owner, self.project)["invite"]
        peer = provision_account(self.store, "peer", invite=invite)["id"]
        note = self.memory.add_note(peer, self.project, "Public project decision")
        self.assertEqual(
            self.memory.snapshot(self.owner, self.project)["notes"][0]["body"],
            "Public project decision",
        )
        self.assertEqual(self.memory.agent_context(self.project)[0]["author_id"], peer)
        self.assertEqual(
            self.memory.delete_note(self.owner, self.project, note["id"]),
            {"deleted": True},
        )
        self.assertEqual(self.memory.snapshot(peer, self.project)["notes"], [])

    def test_nonmember_cannot_read_or_write_memory(self):
        outsider = provision_account(
            self.store,
            "outsider",
            invite=self.store.invite(self.owner, self.project)["invite"],
        )["id"]
        with self.store.connect() as db:
            db.execute(
                "DELETE FROM project_users WHERE project_id=? AND user_id=?",
                (self.project, outsider),
            )
        with self.assertRaises(PermissionError):
            self.memory.snapshot(outsider, self.project)
        with self.assertRaises(PermissionError):
            self.memory.add_note(outsider, self.project, "secret")

    def test_private_turn_does_not_add_shared_note(self):
        # Even a malicious structured reply cannot promote a private statement.
        with self.store.connect() as db:
            db.execute("INSERT INTO nodes(name,owner_id) VALUES(?,?)", ("local", self.owner))
            db.execute(
                """INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,
                model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "agent",
                    self.project,
                    "Agent",
                    self.owner,
                    "local",
                    "local-project",
                    "model",
                    "medium",
                    60,
                ),
            )
        self.store.set_admin(self.owner, self.project, "agent")
        task = self.store.task(
            self.owner,
            {
                "project": self.project,
                "agent": "agent",
                "title": "Private",
                "instructions": "Reply",
                "acceptance": "Reply",
                "validation": "Review",
                "private_turn": True,
                "reply_to": self.owner,
            },
        )["id"]
        report = json.dumps(
            {
                "reply": "private answer",
                "done": True,
                "messages": [],
                "tasks": [],
                "executions": [],
                "memory_notes": ["private information"],
            }
        )
        with self.store.connect() as db:
            db.execute(
                "UPDATE tasks SET state=?,report=? WHERE id=?",
                ("needs_review", report, task),
            )
        Coordinator(self.store).process_completed(task)
        snapshot = self.memory.snapshot(self.owner, self.project)
        self.assertEqual(snapshot["notes"], [])
        self.assertEqual(snapshot["candidates"], [])

    def add_admin(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO nodes(name,owner_id) VALUES(?,?)", ("local", self.owner))
            db.execute(
                """INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout)
                VALUES('agent',?,'Agent',?,'local','local-project','model','medium',60)""",
                (self.project, self.owner),
            )
        self.store.set_admin(self.owner, self.project, "agent")

    def add_candidate_rows(self, *bodies):
        self.add_admin()
        with self.store.connect() as db:
            source = self.store._message(db, self.project, self.owner, "human", "Public project standards")["id"]
        task = self.store.task(
            self.owner,
            {
                "project": self.project,
                "agent": "agent",
                "title": "Record standards",
                "instructions": "Propose durable project rules",
                "acceptance": "Only durable rules",
                "validation": "Review in UI",
                "source_message_id": source,
            },
        )["id"]
        with self.store.connect() as db:
            db.execute(
                "UPDATE tasks SET state='needs_review',report=? WHERE id=?",
                (json.dumps({"reply": "Proposed standards", "memory_notes": bodies}), task),
            )
        coordinator = Coordinator(self.store)
        coordinator.process_completed(task)
        coordinator.process_completed(task)
        candidates = self.memory.snapshot(self.owner, self.project)["candidates"]
        self.assertEqual([row["body"] for row in candidates], list(dict.fromkeys(bodies)))
        self.assertTrue(all(row["source_task_id"] == task and row["source_message_id"] == source for row in candidates))
        return [row["id"] for row in candidates]

    def test_public_candidates_are_injected_only_after_member_confirms(self):
        ids = self.add_candidate_rows("Nightly lint", "Nightly lint", "Distinct proposal")
        self.assertEqual(self.memory.agent_context(self.project), [])
        self.memory.review_notes(self.owner, self.project, ids[:1], "keep", kind="rule")
        self.memory.review_notes(self.owner, self.project, ids[:1], "keep", kind="rule")
        self.memory.review_notes(self.owner, self.project, ids[1:], "archive")
        self.assertEqual([row["body"] for row in self.memory.agent_context(self.project)], ["Nightly lint"])
        result = self.memory.snapshot(self.owner, self.project)
        self.assertEqual(result["notes"][0]["kind"], "rule")
        self.assertEqual(result["notes"][0]["reviewed_by"], self.owner)
        self.assertEqual([row["body"] for row in result["archived"]], ["Distinct proposal"])
        self.assertEqual(result["candidates"], [])

    def test_merge_retains_both_sources_and_handles_retries(self):
        ids = self.add_candidate_rows("Run the checker", "Review output")
        options = {"body": "Check changes and review the checker output", "kind": "rule"}
        merged = self.memory.review_notes(self.owner, self.project, ids, "merge", **options)
        self.assertEqual(
            merged, self.memory.review_notes(self.owner, self.project, list(reversed(ids)), "merge", **options)
        )
        result = self.memory.snapshot(self.owner, self.project)
        self.assertEqual([row["id"] for row in result["notes"][0]["sources"]], ids)
        self.assertEqual([row["merged_into"] for row in result["archived"]], merged["ids"] * 2)
        self.assertEqual(len(self.memory.agent_context(self.project)), 1)
        self.assertEqual(len(result["candidates"]), 0)

    def test_delegate_permission_is_project_scoped_and_rechecked(self):
        ids = self.add_candidate_rows("Use the shared plan", "Require review")
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO account_delegates VALUES(?,?,'delegate',?,'[]',0)",
                ("key", self.owner, json.dumps(["collaboration.task"])),
            )
        with self.assertRaises(PermissionError):
            self.memory.review_notes(self.owner, self.project, ids[:1], "keep", delegate="key")
        with self.store.connect() as db:
            db.execute("INSERT INTO mcp_approval_policies VALUES(?,?,'memory')", (self.project, self.owner))
        self.memory.review_notes(self.owner, self.project, ids[:1], "keep", delegate="key")
        with self.store.connect() as db:
            db.execute("UPDATE account_delegates SET revoked=1 WHERE public_key='key'")
        with self.assertRaises(PermissionError):
            self.memory.review_notes(self.owner, self.project, ids[1:], "archive", delegate="key")

    def test_archived_project_keeps_history_and_rejects_changes(self):
        candidates = self.add_candidate_rows("Awaiting review")
        note = self.memory.add_note(self.owner, self.project, "Keep history")
        with self.store.connect() as db:
            db.execute("UPDATE projects SET archived_at=1 WHERE id=?", (self.project,))
        result = self.memory.snapshot(self.owner, self.project)
        self.assertEqual(result["notes"][0]["id"], note["id"])
        self.assertEqual(result["candidates"][0]["id"], candidates[0])
        with self.assertRaises(ValueError):
            self.memory.add_note(self.owner, self.project, "Blocked")
        with self.assertRaises(ValueError):
            self.memory.delete_note(self.owner, self.project, note["id"])
        with self.assertRaises(ValueError):
            self.memory.review_notes(self.owner, self.project, candidates, "keep")

    def test_v13_upgrade_preserves_manual_notes_and_hides_private_legacy_sources(self):
        with self.store.connect() as db:
            db.execute("DROP TABLE project_memory_notes")
            db.execute(
                """CREATE TABLE project_memory_notes(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL,
                    author_id TEXT NOT NULL,body TEXT NOT NULL,created_at REAL NOT NULL,source_task_id TEXT)"""
            )
            db.execute("ALTER TABLE projects DROP COLUMN archived_at")
            db.execute("DROP TABLE mcp_approval_policies")
            db.executescript(SCHEMA_V9)
            db.execute("INSERT INTO mcp_approval_policies VALUES(?,?,'permission')", (self.project, self.owner))
            db.execute("PRAGMA user_version=13")
            for id_, contract in (("old-public", {}), ("old-private", {"reply_to": self.owner})):
                db.execute(
                    """INSERT INTO tasks(id,project_id,agent_id,title,contract,state,created_at)
                    VALUES(?,?,'legacy-agent','Old work',?,'needs_review',1)""",
                    (id_, self.project, json.dumps(contract)),
                )
            db.executemany(
                "INSERT INTO project_memory_notes(project_id,author_id,body,created_at,source_task_id) VALUES(?,?,?,1,?)",
                [
                    (self.project, self.owner, "Manual note", None),
                    (self.project, "legacy-agent", "Review this", "old-public"),
                    (self.project, "legacy-agent", "Private content", "old-private"),
                ],
            )
        migrated = migrate_database(self.store.database)
        self.assertEqual((migrated["from"], migrated["to"]), (13, 14))
        snapshot = self.memory.snapshot(self.owner, self.project)
        self.assertEqual([row["body"] for row in snapshot["notes"]], ["Manual note"])
        self.assertEqual([row["body"] for row in snapshot["candidates"]], ["Review this"])
        self.assertEqual(snapshot["archived"], [])
        self.assertEqual([row["body"] for row in self.memory.agent_context(self.project)], ["Manual note"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT kind FROM mcp_approval_policies").fetchone()[0], "permission")
            db.execute("INSERT INTO mcp_approval_policies VALUES(?,?,'memory')", (self.project, self.owner))
            self.assertIsNone(db.execute("SELECT archived_at FROM projects WHERE id=?", (self.project,)).fetchone()[0])
        self.assertFalse(migrate_database(self.store.database)["migrated"])

    def test_shared_note_survives_switching_between_group_and_private_contexts(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO nodes(name,owner_id) VALUES(?,?)", ("local", self.owner))
            db.execute(
                """INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,
                model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "agent",
                    self.project,
                    "Agent",
                    self.owner,
                    "local",
                    "local-project",
                    "model",
                    "medium",
                    60,
                ),
            )
            agent = dict(db.execute("SELECT * FROM agents WHERE id=?", ("agent",)).fetchone())
        self.memory.add_note(self.owner, self.project, "Shared decision across chats")
        coordinator = Coordinator(self.store)
        for contract in (
            {"private_turn": False},
            {"private_turn": True, "reply_to": self.owner},
        ):
            context = coordinator.agent_context(agent, {"contract": json.dumps(contract), "cutoff": 0})
            self.assertIn("Shared decision across chats", context)


if __name__ == "__main__":
    unittest.main()
