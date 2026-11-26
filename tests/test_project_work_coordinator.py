import base64
import hashlib
import json
import tempfile
import unittest
import uuid
import time
from pathlib import Path

from agent_harness.account_backend import AccountBackend
from agent_harness.attachments import Attachments
from agent_harness.broker import PullBackend, digest
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

    def test_project_tools_commit_before_reply_and_system_manages_revision(self):
        token = "registered-device-token-for-test-only"
        with self.store.connect() as db:
            db.execute("INSERT INTO pull_devices VALUES('node',?,?)", (digest(token), time.time()))
            db.execute("INSERT INTO pull_bindings VALUES('admin','[\"discuss\"]','[]')")
        self.backend = PullBackend(self.store)
        self.coordinator = Coordinator(self.store, self.backend)

        def claim():
            self.coordinator.tick()
            jobs = self.backend.device_api("/api/device/poll", {"wait_seconds": 0}, token)["jobs"]
            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            lease = {"job_id": job["job_id"], "lease_id": job["lease_id"]}
            self.backend.device_api("/api/device/report", {**lease, "state": "running", "result": {}}, token)
            return lease

        def call(job, action):
            return self.backend.device_api("/api/device/project-tool", {**job, "action": action}, token)["result"]

        def finish(job, **answer):
            self.backend.device_api(
                "/api/device/report",
                {**job, "state": "needs_review", "result": {"report": json.dumps(answer)}},
                token,
            )
            self.coordinator.collect()

        spec = {
            "title": "登录修复",
            "instructions": "重新连接再登录",
            "deliverable": "测试结果",
            "acceptance": "重连成功",
            "validation": "重复登录",
            "agent": "admin",
            "stage": "discuss",
            "allowed_files": "",
            "reference_message_ids": [],
        }
        source = self.store.message(self.owner, self.project, "整理登录任务", mention_agent_id="admin")
        job = claim()
        action = {"kind": "create_draft", "spec": spec}
        with self.assertRaises(PermissionError):
            call({**job, "lease_id": "wrong"}, action)
        invalid = call(job, {"kind": "create_draft", "spec": {"title": "missing required fields"}})
        self.assertEqual(invalid["error_code"], "invalid_arguments")
        self.assertIn("spec.instructions", invalid["message"])
        self.assertEqual(self.work_view()["drafts"], [])
        created = call(job, action)
        self.assertEqual((created["ok"], created["revision"], created["status"]), (True, 1, "draft"))
        self.assertEqual(call(job, action), created)
        self.assertEqual(
            call(job, {**action, "spec": {**spec, "title": "另一个任务"}})["error_code"], "already_submitted"
        )
        self.assertEqual(self.work_view()["items"], [])
        finish(job, reply="", project_action=action)
        self.assertEqual(len(self.work_view()["drafts"]), 1)

        self.command("draft-discuss", draft_id=created["id"], revision=1, body="把延迟写进交付")
        job = claim()
        self.command(
            "revise",
            draft_id=created["id"],
            revision=1,
            **{**spec, "acceptance": "人工修订的验收条件", "reference_message_ids": [source["id"]]},
        )
        change = {"kind": "revise_draft", "changes": {"deliverable": "测试结果和延迟"}}
        conflict = call(job, change)
        self.assertEqual((conflict["error_code"], conflict["current_revision"]), ("revision_conflict", 2))
        self.assertEqual(self.work_view()["drafts"][0]["deliverable"], "测试结果")
        finish(job, reply="", project_action=None)

        self.command("draft-discuss", draft_id=created["id"], revision=2, body="在最新草稿增加延迟数据")
        job = claim()
        self.assertEqual(call(job, {**change, "base_revision": 2})["error_code"], "invalid_arguments")
        revised = call(job, change)
        self.assertEqual((revised["id"], revised["revision"]), (created["id"], 3))
        draft = self.work_view()["drafts"][0]
        self.assertEqual(draft["deliverable"], "测试结果和延迟")
        self.assertEqual(draft["acceptance"], "人工修订的验收条件")
        self.assertIn(source["id"], draft["reference_message_ids"])
        finish(job, reply="", project_action=None)

        work = self.command("confirm", draft_id=created["id"], revision=3)
        job = claim()
        delivery = {"kind": "deliver", "summary": "延迟 4 秒", "links": []}
        receipt = call(job, delivery)
        self.assertEqual((receipt["id"], receipt["status"]), (work["id"], "delivered"))
        self.assertEqual(call(job, delivery), receipt)
        finish(job, reply="", project_action=delivery)
        deliveries = self.work_view()["items"][0]["deliveries"]
        self.assertEqual(
            [(d["message_id"], d["summary"]) for d in deliveries], [(receipt["delivery_message_id"], "延迟 4 秒")]
        )

        sample = b"checked file"
        path = "report.txt"
        self.command("follow-up", work_id=work["id"], body="Attach the sample.")
        job = claim()
        file_delivery = {"kind": "deliver", "summary": "Report", "links": [], "attachments": [path]}
        self.assertEqual(call(job, {**delivery, "links": [path]})["error_code"], "invalid_arguments")
        reserved = call(job, file_delivery)
        self.assertEqual(reserved["status"], "pending_attachment_delivery")
        self.assertEqual(call(job, file_delivery), reserved)
        self.assertEqual(call(job, delivery)["error_code"], "already_submitted")
        record = self.work_view()["items"][0]
        self.assertEqual((record["status"], len(record["deliveries"])), ("running", 1))
        manifest = [
            {"slot": 0, "path": path, "name": path, "size": len(sample), "sha256": hashlib.sha256(sample).hexdigest()}
        ]
        request = self.backend.device_api("/api/device/task-attachment-request", {**job, "manifest": manifest}, token)
        self.assertEqual(self.work_view()["items"][0]["status_reason"], "附件等待设备所有者批准。")
        account = AccountBackend(self.store, self.backend)
        account._operation(self.owner, "", "attachment-decide", {"id": request["id"], "approve": True})
        uploaded = self.backend.device_api(
            "/api/device/task-attachment",
            {**job, "slot": 0, "name": path, "base64": base64.b64encode(sample).decode("ascii")},
            token,
        )
        final_action = {key: value for key, value in file_delivery.items() if key != "attachments"}
        finish(
            job,
            reply="",
            project_action=final_action,
            project_file_delivery=True,
            delivery_attachment_count=1,
            attachment_ids=[uploaded["id"]],
        )
        record = self.work_view()["items"][0]
        self.assertEqual((record["status"], len(record["deliveries"])), ("delivered", 2))
        message = record["deliveries"][-1]["message_id"]
        with self.store.connect() as db:
            self.assertEqual([item["id"] for item in Attachments(self.store).metadata(db, message)], [uploaded["id"]])

        self.command("follow-up", work_id=work["id"], body="Retry the attachment.")
        job = claim()
        self.assertEqual(call(job, file_delivery)["status"], "pending_attachment_delivery")
        finish(
            job,
            reply="",
            project_action=final_action,
            project_file_delivery=True,
            delivery_attachment_count=1,
            delivery_attachment_error="机器所有者拒绝了附件发送",
            attachment_ids=[],
        )
        record = self.work_view()["items"][0]
        self.assertEqual((record["status"], len(record["deliveries"])), ("blocked", 2))
        self.assertIn("附件交付未完成", record["status_reason"])

        self.command("follow-up", work_id=work["id"], body="Retry with a new approval.")
        job = claim()
        self.assertEqual(call(job, file_delivery)["status"], "pending_attachment_delivery")
        finish(
            job,
            reply="",
            project_action={**final_action, "summary": "Different content"},
            project_file_delivery=True,
            delivery_attachment_count=1,
            attachment_ids=[],
        )
        record = self.work_view()["items"][0]
        self.assertEqual((record["status"], len(record["deliveries"])), ("blocked", 2))

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
            "allowed_files": "",
            "reference_message_ids": [],
        }
        self.finish_turn(reply="我已经立项", project_action={"kind": "create_draft", "spec": proposal})
        draft = self.work_view()["drafts"][0]
        self.assertNotIn(
            "我已经立项", [message["body"] for message in self.store.project_view(self.owner, self.project)["messages"]]
        )
        self.assertRegex(draft["id"], r"^draft-[a-f0-9]{32}$")
        self.assertEqual(self.work_view()["items"], [])
        self.assertEqual(draft["reference_message_ids"], [source["id"]])
        self.command("draft-discuss", draft_id=draft["id"], revision=1, body="交付中加上复现步骤")
        self.coordinator.tick()
        self.assertEqual(self.backend.submissions[-1][0]["project_work_drafts"][0]["id"], draft["id"])
        self.finish_turn(
            reply="误填编号",
            project_action={
                "kind": "revise_draft",
                "draft_id": draft["id"],
                "base_revision": 2,
                "spec": proposal,
            },
        )
        self.assertEqual([item["id"] for item in self.work_view()["drafts"]], [draft["id"]])
        self.assertEqual(self.work_view()["drafts"][0]["revision"], 1)
        self.command("draft-discuss", draft_id=draft["id"], revision=1, body="还是把复现步骤加进交付")
        self.coordinator.tick()
        self.finish_turn(
            reply="请核对修订稿",
            project_action={
                "kind": "revise_draft",
                "draft_id": draft["id"],
                "base_revision": 1,
                "spec": {**proposal, "deliverable": "修复和复现步骤"},
            },
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
        self.store.message(self.owner, self.project, "请核对登录工作", mention_agent_id="admin")
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.finish_turn(
            reply="已经交付第二条", project_action={"kind": "deliver", "summary": "虚假的交付", "links": []}
        )
        self.assertFalse(any(item["deliveries"] for item in self.work_view()["items"]))
        self.assertIn("未通过校验", self.store.project_view(self.owner, self.project)["messages"][-1]["body"])
        follow_up = "登录修复再测切换 Wi-Fi"
        self.store.message(self.owner, self.project, follow_up, mention_agent_id="admin")
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.finish_turn(
            reply="已经补充",
            project_action={"kind": "follow_up", "work_id": confirmed["id"], "instruction": "再测切换 Wi-Fi"},
        )
        items = {item["id"]: item for item in self.work_view()["items"]}
        self.assertTrue(any(row["body"] == "再测切换 Wi-Fi" for row in items[confirmed["id"]]["inputs"]))
        self.assertFalse(any(row["body"] == "再测切换 Wi-Fi" for row in items[other["id"]]["inputs"]))

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
        self.finish_turn(reply="Done", project_action={"kind": "deliver", "summary": "Done", "links": []})
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
        self.finish_turn(project_action={"kind": "wait", "question": "Which logs should I check?"})
        self.assertEqual(self.work_view()["items"][0]["status"], "waiting")
        self.command("follow-up", work_id=work["id"], body="Check the traces I sent")
        self.coordinator.tick()
        self.finish_turn(project_action={"kind": "deliver", "summary": "Private result", "links": []})
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
                    "project_action": {
                        "kind": "deliver",
                        "summary": "Guide updated",
                        "links": ["https://example.com/GUIDE.md"],
                    },
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
        self.assertEqual(json.loads(delivery["links"]), ["https://example.com/GUIDE.md"])
        self.assertEqual(message["body"], "Guide updated")


if __name__ == "__main__":
    unittest.main()
