import uuid
import unittest

from agent_harness.project_work import ProjectWork
from agent_harness.storage import SCHEMA_VERSION, migrate_database
from tests import test_collaboration as fixtures


class ProjectWorkStoreTests(unittest.TestCase):
    setUp = fixtures.CollaborationTests.setUp
    tearDown = fixtures.CollaborationTests.tearDown
    add_agent = fixtures.CollaborationTests.add_agent

    def set_agents(self):
        admin = self.add_agent(self.owner["id"], "backend-a", "Admin")
        worker = self.add_agent(self.owner["id"], "backend-b", "Worker")
        with self.store.connect() as db:
            db.execute("UPDATE agents SET configuring=0 WHERE id IN (?,?)", (admin, worker))
            db.execute("UPDATE projects SET admin_agent_id=? WHERE id=?", (admin, self.project))
        return admin, worker

    def payload(self, worker, **extra):
        value = {
            "project": self.project,
            "action": "create",
            "client_request_id": str(uuid.uuid4()),
            "title": "Build it",
            "instructions": "Implement the requested behavior",
            "deliverable": "A tested change",
            "acceptance": "The focused tests pass",
            "validation": "python -m unittest",
            "agent": worker,
            "allowed_files": "src/example.py",
            "stage": "implement",
            "visibility": "group",
        }
        value.update(extra)
        return value

    def test_direct_creation_is_idempotent_and_schedules_stable_work(self):
        _, worker = self.set_agents()
        message = self.store.message(self.owner["id"], self.project, "source")
        request = self.payload(worker, reference_message_ids=[message["id"]])
        work = ProjectWork(self.store).command(self.owner["id"], request)
        duplicate = ProjectWork(self.store).command(self.owner["id"], request)
        self.assertEqual(duplicate["id"], work["id"])

        with self.store.connect() as db:
            scheduled = ProjectWork(self.store).schedule(db)
            self.assertEqual(len(scheduled), 1)
            context = ProjectWork(self.store).execution_context(db, scheduled[0]["id"])
            self.assertEqual(context["work_id"], work["id"])
            self.assertEqual(context["reference_message_ids"], [message["id"]])
            task = db.execute("SELECT contract FROM tasks WHERE id=?", (scheduled[0]["task_id"],)).fetchone()[0]
            self.assertIn('"project_work_execution_id"', task)

    def test_admin_proposal_requires_exact_revision_and_rechecks_reference_audience(self):
        admin, worker = self.set_agents()
        source = self.store.message(self.owner["id"], self.project, "please prepare", mention_agent_id=admin)
        private = self.store.message(self.owner["id"], self.project, "secret", admin)
        spec = self.payload(worker)
        for key in ("project", "action", "client_request_id"):
            spec.pop(key)
        with self.store.connect() as db:
            project = dict(db.execute("SELECT * FROM projects WHERE id=?", (self.project,)).fetchone())
            admin_row = db.execute("SELECT * FROM agents WHERE id=?", (admin,)).fetchone()
            source_row = db.execute("SELECT * FROM messages WHERE id=?", (source["id"],)).fetchone()
            with self.assertRaises(PermissionError):
                ProjectWork(self.store).propose(
                    db, project, admin_row, source_row, {**spec, "reference_message_ids": [private["id"]]}
                )
            draft = ProjectWork(self.store).propose(db, project, admin_row, source_row, spec)
            revised = ProjectWork(self.store).propose(
                db,
                project,
                admin_row,
                source_row,
                {**spec, "draft_id": draft["id"], "revision": 1, "instructions": "Revised exact instructions"},
            )
            self.assertEqual(revised["revision"], 2)
            with self.assertRaises(ValueError):
                ProjectWork(self.store).propose(
                    db, project, admin_row, source_row, {**spec, "draft_id": draft["id"], "revision": 1}
                )

        with self.assertRaises(ValueError):
            ProjectWork(self.store).command(
                self.owner["id"],
                {
                    "project": self.project,
                    "action": "confirm",
                    "client_request_id": str(uuid.uuid4()),
                    "draft_id": draft["id"],
                    "revision": 3,
                },
            )
        confirmed = ProjectWork(self.store).command(
            self.owner["id"],
            {
                "project": self.project,
                "action": "confirm",
                "client_request_id": str(uuid.uuid4()),
                "draft_id": draft["id"],
                "revision": 2,
            },
        )
        self.assertEqual(confirmed["status"], "queued")

    def test_delivery_and_acceptance_are_explicit_human_steps(self):
        _, worker = self.set_agents()
        work = ProjectWork(self.store).command(self.owner["id"], self.payload(worker))
        with self.store.connect() as db:
            execution = ProjectWork(self.store).schedule(db)[0]
            delivery_message = self.store._message(
                db, self.project, worker, "agent", "done", None, execution["task_id"]
            )
            result = ProjectWork(self.store).finish(
                db,
                execution["id"],
                "delivered",
                {"summary": "Complete", "links": ["src/example.py"], "message_id": delivery_message["id"]},
            )
            self.assertEqual(result["status"], "delivered")
        accepted = ProjectWork(self.store).command(
            self.owner["id"],
            {
                "project": self.project,
                "action": "accept",
                "client_request_id": str(uuid.uuid4()),
                "work_id": work["id"],
            },
        )
        self.assertEqual(accepted["status"], "accepted")

    def test_public_state_changes_project_safe_cards_and_delivery_reuses_message(self):
        _, worker = self.set_agents()
        work = ProjectWork(self.store).command(self.owner["id"], self.payload(worker))
        with self.store.connect() as db:
            created = db.execute(
                "SELECT * FROM project_work_cards WHERE record_id=? AND event='created'", (work["id"],)
            ).fetchone()
            self.assertIsNotNone(created)
            message = db.execute("SELECT * FROM messages WHERE id=?", (created["message_id"],)).fetchone()
            self.assertEqual(message["sender_type"], "system")
            self.assertNotIn("Implement", message["body"])
            self.assertEqual(
                ProjectWork(self.store).card_for_message(db, self.owner["id"], self.project, message["id"]),
                {"kind": "work", "id": work["id"], "event": "created"},
            )
            execution = ProjectWork(self.store).schedule(db)[0]
            contract = db.execute("SELECT contract FROM tasks WHERE id=?", (execution["task_id"],)).fetchone()[0]
            self.assertTrue(__import__("json").loads(contract)["project_action_v2"])
            delivery = self.store._message(
                db, self.project, worker, "agent", "published result", None, execution["task_id"]
            )
            ProjectWork(self.store).finish(
                db,
                execution["id"],
                "delivered",
                {"summary": "Complete", "links": [], "message_id": delivery["id"]},
            )
            delivered = db.execute(
                "SELECT * FROM project_work_cards WHERE record_id=? AND event='delivered'", (work["id"],)
            ).fetchone()
            self.assertEqual(delivered["message_id"], delivery["id"])
            db.execute("DROP TABLE project_tool_results")
            db.execute("DROP TABLE project_work_cards")
            db.execute("PRAGMA user_version=11")
        result = migrate_database(self.store.database)
        self.assertEqual((result["from"], result["to"]), (11, SCHEMA_VERSION))
        self.assertTrue(result["backup"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM project_tool_results").fetchone()[0], 0)
            self.assertEqual(
                ProjectWork(self.store).card_for_message(db, self.owner["id"], self.project, delivery["id"]),
                {"kind": "work", "id": work["id"], "event": "delivered"},
            )

    def test_private_work_cards_stay_in_private_thread_and_duplicate_confirmation_is_quiet(self):
        admin, worker = self.set_agents()
        private = ProjectWork(self.store).command(
            self.owner["id"],
            self.payload(
                admin,
                visibility="private",
                conversation_id=self.store._pair_conversation("human_agent", self.owner["id"], admin),
            ),
        )
        with self.store.connect() as db:
            card = db.execute("SELECT * FROM project_work_cards WHERE record_id=?", (private["id"],)).fetchone()
            message = db.execute("SELECT * FROM messages WHERE id=?", (card["message_id"],)).fetchone()
            self.assertEqual(
                (message["recipient_id"], message["conversation_id"]),
                (self.owner["id"], private["conversation_id"]),
            )

        source = self.store.message(self.owner["id"], self.project, "prepare", mention_agent_id=admin)
        spec = self.payload(worker)
        for key in ("project", "action", "client_request_id"):
            spec.pop(key)
        with self.store.connect() as db:
            project = dict(db.execute("SELECT * FROM projects WHERE id=?", (self.project,)).fetchone())
            admin_row = db.execute("SELECT * FROM agents WHERE id=?", (admin,)).fetchone()
            draft = ProjectWork(self.store).propose(db, project, admin_row, source["id"], spec)
        confirmation = {
            "project": self.project,
            "action": "confirm",
            "client_request_id": str(uuid.uuid4()),
            "draft_id": draft["id"],
            "revision": 1,
        }
        work = ProjectWork(self.store).command(self.owner["id"], confirmation)
        ProjectWork(self.store).command(self.owner["id"], {**confirmation, "client_request_id": str(uuid.uuid4())})
        with self.store.connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM project_work_cards WHERE record_id=? AND event='confirmed'", (work["id"],)
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_follow_up_arriving_during_execution_requeues_after_delivery(self):
        admin, worker = self.set_agents()
        work = ProjectWork(self.store).command(self.owner["id"], self.payload(worker))
        with self.store.connect() as db:
            execution = ProjectWork(self.store).schedule(db)[0]
        ProjectWork(self.store).command(
            self.owner["id"],
            {
                "project": self.project,
                "action": "follow-up",
                "client_request_id": str(uuid.uuid4()),
                "work_id": work["id"],
                "body": "Also cover the login case",
            },
        )
        with self.store.connect() as db:
            delivery_message = self.store._message(
                db, self.project, worker, "agent", "first pass", None, execution["task_id"]
            )
            result = ProjectWork(self.store).finish(
                db,
                execution["id"],
                "delivered",
                {"summary": "First pass", "links": [], "message_id": delivery_message["id"]},
            )
            self.assertEqual(result["status"], "queued")

    def test_scope_revision_is_pinned_per_execution_and_stale_edits_fail(self):
        _, worker = self.set_agents()
        work = ProjectWork(self.store).command(self.owner["id"], self.payload(worker))
        self.assertEqual(work["revision"], 1)
        self.assertEqual(work["inputs"], [])
        with self.store.connect() as db:
            first = ProjectWork(self.store).schedule(db)[0]
            self.assertEqual(
                ProjectWork(self.store).execution_context(db, first["id"])["instructions"],
                "Implement the requested behavior",
            )
        updated = ProjectWork(self.store).command(
            self.owner["id"],
            {
                "project": self.project,
                "action": "update-scope",
                "client_request_id": str(uuid.uuid4()),
                "work_id": work["id"],
                "revision": 1,
                "instructions": "Implement login and retry behavior",
            },
        )
        self.assertEqual(updated["revision"], 2)
        with self.assertRaises(ValueError):
            ProjectWork(self.store).command(
                self.owner["id"],
                {
                    "project": self.project,
                    "action": "update-scope",
                    "client_request_id": str(uuid.uuid4()),
                    "work_id": work["id"],
                    "revision": 1,
                    "title": "Stale title",
                },
            )
        with self.store.connect() as db:
            old = ProjectWork(self.store).execution_context(db, first["id"])
            self.assertEqual(old["revision"], 1)
            self.assertEqual(old["instructions"], "Implement the requested behavior")
            delivery = self.store._message(db, self.project, worker, "agent", "old scope done", None, first["task_id"])
            self.assertEqual(
                ProjectWork(self.store).finish(
                    db,
                    first["id"],
                    "delivered",
                    {"summary": "Old scope", "links": [], "message_id": delivery["id"]},
                )["status"],
                "queued",
            )
            db.execute("UPDATE tasks SET state='needs_review',processed=1 WHERE id=?", (first["task_id"],))
            second = ProjectWork(self.store).schedule(db)[0]
            new = ProjectWork(self.store).execution_context(db, second["id"])
            self.assertEqual(new["revision"], 2)
            self.assertEqual(new["instructions"], "Implement login and retry behavior")


if __name__ == "__main__":
    unittest.main()
