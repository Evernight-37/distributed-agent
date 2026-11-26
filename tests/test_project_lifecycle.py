import json
import unittest
import uuid

from agent_harness.coordinator import Coordinator
from agent_harness.project_lifecycle import ProjectLifecycle
from agent_harness.project_memory import ProjectMemory
from agent_harness.project_work import ProjectWork
from tests import test_collaboration as fixtures


class ProjectLifecycleTests(unittest.TestCase):
    setUp = fixtures.CollaborationTests.setUp
    tearDown = fixtures.CollaborationTests.tearDown
    add_agent = fixtures.CollaborationTests.add_agent

    def assign(self, agent, **extra):
        return self.store.task(
            self.owner["id"],
            {
                "project": self.project,
                "agent": agent,
                "title": "Trial task",
                "instructions": "Review the demo",
                "acceptance": "Pass its checks",
                "validation": "Run its test suite",
                **extra,
            },
        )["id"]

    def test_archive_stops_dispatch_and_leaves_existing_history_available(self):
        lifecycle = ProjectLifecycle(self.store)
        owner, guest = self.owner["id"], self.other["id"]
        agent = self.add_agent(owner, "backend-a", "Admin")
        with self.store.connect() as db:
            db.execute("UPDATE agents SET configuring=0 WHERE id=?", (agent,))
        message = self.store.message(owner, self.project, "A public note")
        task = self.assign(agent)
        with self.assertRaises(PermissionError):
            lifecycle.command(guest, self.project, "archive")
        archived = lifecycle.command(owner, self.project, "archive")["archived_at"]
        self.assertEqual(lifecycle.command(owner, self.project, "archive")["archived_at"], archived)
        view = self.store.project_view(guest, self.project)
        self.assertEqual(view["project"]["archived_at"], archived)
        self.assertIn(message["id"], {row["id"] for row in view["messages"]})
        with self.assertRaisesRegex(ValueError, "已归档"):
            self.store.message(guest, self.project, "A new message")
        with self.assertRaisesRegex(ValueError, "已归档"):
            self.assign(agent)
        coordinator = Coordinator(self.store, self.backend)
        coordinator.dispatch()
        self.assertEqual(self.backend.submissions, [])

        self.assertIsNone(lifecycle.command(owner, self.project, "restore")["archived_at"])
        coordinator.dispatch()
        self.assertEqual(len(self.backend.submissions), 1)
        lifecycle.command(owner, self.project, "archive")
        coordinator.collect()
        self.assertEqual(self.store.project_view(owner, self.project)["tasks"][0]["id"], task)
        self.assertEqual(self.store.project_view(owner, self.project)["tasks"][0]["state"], "needs_review")

    def test_delete_checks_confirmation_and_busy_jobs_and_cascades_one_project(self):
        lifecycle = ProjectLifecycle(self.store)
        owner = self.owner["id"]
        second = self.store.project_create(owner, "Keep")
        self.store.message(owner, second["id"], "Keep this")
        agent = self.add_agent(owner, "backend-a", "Admin")
        with self.store.connect() as db:
            db.execute("UPDATE agents SET configuring=0 WHERE id=?", (agent,))
        original = self.store.message(owner, self.project, "A project reference")
        work = ProjectWork(self.store)
        work.command(
            owner,
            {
                "project": self.project,
                "action": "create",
                "client_request_id": str(uuid.uuid4()),
                "title": "First task",
                "instructions": "Build it",
                "deliverable": "A tested change",
                "acceptance": "Pass checks",
                "validation": "Run the tests",
                "agent": agent,
                "stage": "discuss",
                "reference_message_ids": [original["id"]],
            },
        )
        ProjectMemory(self.store).add_note(owner, self.project, "Keep each note focused", kind="rule")
        with self.store.connect() as db:
            task = work.schedule(db)[0]["task_id"]
            db.execute("UPDATE tasks SET state='running' WHERE id=?", (task,))
        with self.assertRaisesRegex(ValueError, "先归档"):
            lifecycle.command(owner, self.project, "delete-preview")
        lifecycle.command(owner, self.project, "archive")
        preview = lifecycle.command(owner, self.project, "delete-preview")
        self.assertEqual(preview["counts"]["memory"], 1)
        self.assertEqual(preview["counts"]["tasks"], 1)
        self.assertTrue(preview["busy"])
        with self.assertRaisesRegex(ValueError, "项目全名"):
            lifecycle.command(owner, self.project, "delete", "wrong")
        with self.assertRaisesRegex(ValueError, "处理中"):
            lifecycle.command(owner, self.project, "delete", "研究项目")
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET state='failed' WHERE id=?", (task,))
        self.assertEqual(lifecycle.command(owner, self.project, "delete", "研究项目"), {"deleted": True})
        with self.store.connect() as db:
            for table in (
                "project_work_item_revisions",
                "project_work_executions",
                "task_trace",
                "project_memory_notes",
            ):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.store.project_view(owner, second["id"])["messages"][0]["body"], "Keep this")

    def test_people_disambiguation_and_private_usage_visibility(self):
        owner, guest = self.owner["id"], self.other["id"]
        agent = self.add_agent(owner, "backend-a", "Helper")
        with self.store.connect() as db:
            db.execute("UPDATE agents SET configuring=0 WHERE id=?", (agent,))
        message = self.store.message(owner, self.project, "Public")
        self.assertEqual(self.store.project_view(guest, self.project)["messages"][0]["sender_name"], "owner")
        with self.store.connect() as db:
            db.execute("UPDATE users SET username=? WHERE id=?", ("owner · " + guest[-8:], guest))
        viewed = self.store.project_view(guest, self.project)
        self.assertEqual(
            next(row for row in viewed["messages"] if row["id"] == message["id"])["sender_name"],
            "owner · " + owner[-8:],
        )
        task = self.assign(agent, audience="private")
        result = {"started_at": 100, "finished_at": 102, "usage": {"input_tokens": 100, "cached_input_tokens": 25}}
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET job='job-private' WHERE id=?", (task,))
            db.execute(
                "INSERT INTO pull_jobs(id,node,agent_id,kind,request,state,lease_id,result,created_at) "
                "VALUES('job-private','lab',?,'task','{}','completed','lease',?,100)",
                (agent, json.dumps(result)),
            )
        self.assertEqual(self.store.project_view(owner, self.project)["tasks"][0]["run_details"], result)
        self.assertEqual(self.store.project_view(guest, self.project)["tasks"], [])
