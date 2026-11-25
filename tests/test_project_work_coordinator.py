import json
import tempfile
import unittest
import uuid
from pathlib import Path

from agent_harness.collaboration_store import Store
from agent_harness.coordinator import Coordinator
from agent_harness.project_work import ProjectWork
from tests.helpers import provision_account


class Backend:
    durable_ids = True

    def __init__(self):
        self.submissions = []
        self.result = {"status": "running"}

    def submit(self, project, context, stage, scope, acceptance, validation, **kwargs):
        self.submissions.append((json.loads(context), kwargs))
        return {"job": kwargs["job_id"]}

    def status(self, job, fetch=True):
        return self.result


class ProjectWorkCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.owner = provision_account(self.store, "owner", bootstrap=True)["id"]
        self.project = self.store.project_create(self.owner, "project")["id"]
        with self.store.connect() as db:
            db.execute("INSERT INTO nodes(name,owner_id) VALUES('node',?)", (self.owner,))
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout,enabled) "
                "VALUES('admin',?,'Admin',?,'node','admin','model','low',30,1)",
                (self.project, self.owner),
            )
        self.store.set_admin(self.owner, self.project, "admin")
        self.backend = Backend()
        self.coordinator = Coordinator(self.store, self.backend)

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, action, **fields):
        return ProjectWork(self.store).command(
            self.owner, {"project": self.project, "action": action, "client_request_id": str(uuid.uuid4()), **fields}
        )

    def finish_turn(self, **answer):
        self.backend.result = {"status": "needs_review", "report": json.dumps(answer)}
        self.coordinator.collect()
        self.backend.result = {"status": "running"}

    def work_view(self):
        with self.store.connect() as db:
            return ProjectWork(self.store).view(db, self.owner, self.project)

    def test_admin_prepares_revises_and_routes_followup_after_exact_confirmation(self):
        source = self.store.message(self.owner, self.project, "断网后不能登录", mention_agent_id="admin")
        unrelated = self.store.message(self.owner, self.project, "打包出错了")
        self.coordinator.tick()
        proposal = {
            "title": "登录修复",
            "instructions": "修复断网重连后无法登录",
            "deliverable": "登录修复",
            "acceptance": "恢复网络后可登录",
            "validation": "复现后重跑登录测试",
            "agent": "admin",
            "stage": "discuss",
        }
        self.finish_turn(reply="请核对草稿", work_proposal=proposal)
        draft = self.work_view()["drafts"][0]
        self.assertEqual(self.work_view()["items"], [])
        self.assertEqual(draft["reference_message_ids"], [source["id"]])
        self.command("draft-discuss", draft_id=draft["id"], revision=1, body="交付中加上复现步骤")
        self.coordinator.tick()
        self.assertEqual(self.backend.submissions[-1][0]["project_work_drafts"][0]["id"], draft["id"])
        self.finish_turn(
            reply="请核对修订稿",
            work_proposal={**proposal, "id": draft["id"], "revision": 1, "deliverable": "修复和复现步骤"},
        )
        revised = self.work_view()["drafts"][0]
        self.assertEqual((revised["revision"], revised["id"]), (2, draft["id"]))
        self.assertIn(source["id"], revised["reference_message_ids"])
        self.assertNotIn(unrelated["id"], revised["reference_message_ids"])
        with self.assertRaises(ValueError):
            self.command("confirm", draft_id=draft["id"], revision=1)
        confirmed = self.command("draft-discuss", draft_id=draft["id"], revision=2, body="按这一版创建")
        duplicate = self.command("confirm", draft_id=draft["id"], revision=2)
        self.assertEqual(duplicate["id"], confirmed["id"])
        other = self.command("create", **{**proposal, "title": "打包故障"})
        follow_up = "登录修复再测切换 Wi-Fi"
        self.store.message(self.owner, self.project, follow_up, mention_agent_id="admin")
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.finish_turn(reply="已经补充", work_follow_up={"work_id": confirmed["id"]})
        items = {item["id"]: item for item in self.work_view()["items"]}
        self.assertTrue(any(row["body"] == follow_up for row in items[confirmed["id"]]["inputs"]))
        self.assertFalse(any(row["body"] == follow_up for row in items[other["id"]]["inputs"]))

    def test_one_time_permission_continues_the_same_work_without_granting_later_runs(self):
        work = self.command(
            "create",
            title="Fix it",
            instructions="Check the approved external fixture",
            deliverable="Test result",
            acceptance="Results recorded",
            validation="Read test logs",
            agent="admin",
            stage="implement",
            allowed_files="tests/example.py",
        )
        self.coordinator.tick()
        first = self.backend.submissions[-1][0]
        self.finish_turn(reply="Waiting for permission", request={"reason": "Read the protected fixture"})
        with self.store.connect() as db:
            request = db.execute("SELECT id FROM permission_requests").fetchone()[0]
        self.store.permission_decide(self.owner, self.project, request, True)
        self.coordinator.dispatch()
        resumed = self.backend.submissions[-1][0]
        self.assertEqual(resumed["grants"], ["full_access"])
        self.assertEqual(resumed["project_work"]["work_id"], work["id"])
        self.assertNotEqual(resumed["project_work"]["execution_id"], first["project_work"]["execution_id"])
        self.finish_turn(reply="Done", work_delivery={"summary": "Done", "links": []})
        self.command("follow-up", work_id=work["id"], body="Add ordinary details")
        self.coordinator.tick()
        self.assertEqual(self.backend.submissions[-1][0]["grants"], [])

    def test_private_work_keeps_its_source_questions_and_delivery_private(self):
        colleague = provision_account(
            self.store, "colleague", invite=self.store.invite(self.owner, self.project)["invite"]
        )["id"]
        source = self.store.message(self.owner, self.project, "private diagnostics", recipient="admin")
        fields = {
            "instructions": "Review the diagnostics",
            "deliverable": "Written result",
            "acceptance": "Explain the problem",
            "validation": "Confirm the symptom",
            "agent": "admin",
            "source_message_id": source["id"],
        }
        with self.assertRaises(PermissionError):
            self.command("create", title="Public", **fields)
        work = self.command("create", title="Private", visibility="private", **fields)
        self.coordinator.tick()
        self.finish_turn(reply="Which logs should I check?")
        self.assertEqual(self.work_view()["items"][0]["status"], "waiting")
        self.command("follow-up", work_id=work["id"], body="Check the traces I sent")
        self.coordinator.tick()
        self.finish_turn(work_delivery={"summary": "Private result", "links": []})
        delivery = self.work_view()["items"][0]["deliveries"][-1]
        with self.store.connect() as db:
            observer = ProjectWork(self.store).view(db, colleague, self.project)
            message = db.execute("SELECT * FROM messages WHERE id=?", (delivery["message_id"],)).fetchone()
        self.assertEqual(message["conversation_id"], work["conversation_id"])
        self.assertEqual(observer["items"], [])
        self.assertFalse(
            any(m["body"] == "Private result" for m in self.store.project_view(colleague, self.project)["messages"])
        )

    def test_scheduled_work_has_isolated_context_stable_session_and_delivery(self):
        source = self.store.message(self.owner, self.project, "Please update the guide", mention_agent_id="admin")
        work = ProjectWork(self.store).command(
            self.owner,
            {
                "project": self.project,
                "action": "create",
                "client_request_id": str(uuid.uuid4()),
                "title": "Update guide",
                "instructions": "Make the requested documentation change",
                "deliverable": "Updated guide",
                "acceptance": "The guide is accurate",
                "validation": "Review the diff",
                "agent": "admin",
                "allowed_files": "GUIDE.md",
                "stage": "implement",
                "visibility": "group",
                "reference_message_ids": [source["id"]],
            },
        )

        self.coordinator.tick()
        context, kwargs = self.backend.submissions[-1]
        self.assertEqual(kwargs["session_channel"], work["id"])
        self.assertEqual(context["project_work"]["work_id"], work["id"])
        self.assertNotIn("visible_messages", context)
        self.assertNotIn("project_memory", context)
        self.assertNotIn("members", context)

        self.backend.result = {
            "status": "needs_review",
            "report": json.dumps(
                {
                    "reply": "",
                    "work_delivery": {"summary": "Guide updated", "links": ["GUIDE.md"], "message_id": None},
                    "messages": [],
                    "tasks": [],
                    "executions": [],
                    "memory_notes": [],
                }
            ),
        }
        self.coordinator.collect()
        with self.store.connect() as db:
            item = db.execute("SELECT status FROM project_work_items WHERE id=?", (work["id"],)).fetchone()
            delivery = db.execute(
                "SELECT summary,links,message_id FROM project_work_deliveries WHERE work_id=?", (work["id"],)
            ).fetchone()
            message = db.execute("SELECT body FROM messages WHERE id=?", (delivery["message_id"],)).fetchone()
        self.assertEqual(item["status"], "delivered")
        self.assertEqual(delivery["summary"], "Guide updated")
        self.assertEqual(json.loads(delivery["links"]), ["GUIDE.md"])
        self.assertEqual(message["body"], "Guide updated")


if __name__ == "__main__":
    unittest.main()
