import json
import tempfile
import unittest
import uuid
from pathlib import Path

from broker import PullBackend
from collaboration_store import Store
from coordinator import Coordinator


class RecoveryAndMessageIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.user = self.store.signup("owner", "", bootstrap=True)["id"]
        self.project = self.store.project_create(self.user, "project")["id"]

    def agent_and_task(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO nodes VALUES(?,?)", ("node", self.user))
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout,enabled) VALUES(?,?,?,?,?,?,?,?,?,1)",
                ("agent", self.project, "Agent", self.user, "node", "backend", "model", "low", 30),
            )
            project = dict(db.execute("SELECT * FROM projects WHERE id=?", (self.project,)).fetchone())
            agent = dict(db.execute("SELECT * FROM agents WHERE id=?", ("agent",)).fetchone())
            self.store._message(db, self.project, self.user, "human", "trigger")
            task = self.store._task(
                db,
                project,
                agent,
                {
                    "title": "task",
                    "instructions": "work",
                    "acceptance": "done",
                    "validation": "check",
                    "stage": "discuss",
                },
                self.user,
            )["id"]
        return task

    def test_missing_durable_pull_job_converges_failed_after_restart(self):
        task = self.agent_and_task()
        backend = PullBackend(self.store)
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET state='starting',job='job-never-created' WHERE id=?", (task,))
        coordinator = Coordinator(self.store, backend)
        coordinator.recover()
        coordinator.wake_messages()
        with self.store.connect() as db:
            row = db.execute("SELECT state,error,processed FROM tasks WHERE id=?", (task,)).fetchone()
            count = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["processed"], 1)
        self.assertEqual(count, 1)
        self.assertIn("no remote execution", row["error"])
        view = self.store.project_view(self.user, self.project)
        self.assertNotIn("（正在回复…）", [m["body"] for m in view["messages"]])
        self.assertTrue(any("未能回复" in m["body"] for m in view["messages"]))

    def test_existing_durable_pull_job_recovers_running(self):
        task = self.agent_and_task()
        backend = PullBackend(self.store)
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET state='starting',job='job-present' WHERE id=?", (task,))
            db.execute(
                "INSERT INTO pull_jobs(id,node,agent_id,kind,request,state,lease_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("job-present", "node", "agent", "task", "{}", "claimed", "lease", 1),
            )
        Coordinator(self.store, backend).recover()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM tasks WHERE id=?", (task,)).fetchone()[0], "running")

    def test_existing_terminal_pull_job_is_finalized_during_recovery(self):
        task = self.agent_and_task()
        backend = PullBackend(self.store)
        report = json.dumps({"reply": "recovered reply", "done": True, "messages": [], "tasks": [], "executions": []})
        result = json.dumps({"status": "needs_review", "report": report})
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET state='starting',job='job-complete' WHERE id=?", (task,))
            db.execute(
                "INSERT INTO pull_jobs(id,node,agent_id,kind,request,state,lease_id,result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("job-complete", "node", "agent", "task", "{}", "needs_review", "lease", result, 1),
            )
        coordinator = Coordinator(self.store, backend)
        coordinator.recover()
        coordinator.wake_messages()
        with self.store.connect() as db:
            row = db.execute("SELECT state,processed,report FROM tasks WHERE id=?", (task,)).fetchone()
            count = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual((row["state"], row["processed"], row["report"]), ("needs_review", 1, report))
        self.assertEqual(count, 1)
        self.assertIn(
            "recovered reply", [m["body"] for m in self.store.project_view(self.user, self.project)["messages"]]
        )

    def test_human_message_uuid_is_idempotent_and_content_bound(self):
        key = str(uuid.uuid4())
        first = self.store.message(self.user, self.project, "hello", client_message_id=key)
        second = self.store.message(self.user, self.project, "hello", client_message_id=key)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.store.project_view(self.user, self.project)["messages"]), 1)
        with self.assertRaisesRegex(ValueError, "其他内容"):
            self.store.message(self.user, self.project, "changed", client_message_id=key)
        # Old clients remain compatible and intentionally receive distinct rows.
        self.store.message(self.user, self.project, "legacy")
        self.store.message(self.user, self.project, "legacy")
        self.assertEqual(len(self.store.project_view(self.user, self.project)["messages"]), 3)


if __name__ == "__main__":
    unittest.main()
