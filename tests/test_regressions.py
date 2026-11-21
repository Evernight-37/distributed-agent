import json
import unittest
from agent_harness.collaboration_store import uid
from agent_harness.coordinator import Coordinator
from tests import test_collaboration as fixtures


class Regressions(unittest.TestCase):
    setUp = fixtures.CollaborationTests.setUp
    tearDown = fixtures.CollaborationTests.tearDown
    add_agent = fixtures.CollaborationTests.add_agent

    def agents(self):
        a = self.add_agent(self.owner["id"], "backend-a", "管理员")
        b = self.add_agent(self.owner["id"], "backend-b", "同事")
        self.store.set_admin(self.owner["id"], self.project, a)
        return a, b

    def finished(self, agent, report, state="needs_review", private=False, reply_to=None, **extra):
        data = {
            "project": self.project,
            "agent": agent,
            "title": "检查",
            "instructions": "检查目标",
            "stage": "discuss",
            "allowed_files": "只读",
            "acceptance": "回复",
            "validation": "复核",
            "private_turn": private,
            "reply_to": reply_to,
            **extra,
        }
        task = self.store.task(self.owner["id"], data)["id"]
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET state=?,report=? WHERE id=?", (state, report, task))
        return task

    def test_private_conversation_can_delegate_a_task(self):
        """A human DM to the admin must be able to hand work out privately."""
        a, b = self.agents()
        delegation = {
            "agent": b,
            "title": "复核泄漏",
            "instructions": "只给 b 看的内容",
            "stage": "discuss",
            "allowed_files": "只读",
            "acceptance": "给出结论",
            "validation": "复核",
        }
        task = self.finished(
            a,
            json.dumps({"reply": "已安排", "tasks": [delegation]}),
            private=True,
            reply_to=self.owner["id"],
        )
        Coordinator(self.store, self.backend).process_completed(task)
        with self.store.connect() as db:
            delegated = db.execute("SELECT * FROM tasks WHERE agent_id=?", (b,)).fetchone()
        self.assertIsNotNone(delegated)
        contract = json.loads(delegated["contract"])
        self.assertTrue(contract["private_turn"])
        self.assertEqual(contract["reply_to"], a)
        # The contract text never reaches the group or the other human's view.
        for user in (self.owner["id"], self.other["id"]):
            bodies = [m["body"] for m in self.store.project_view(user, self.project)["messages"]]
            self.assertFalse(any("只给 b 看的内容" in body for body in bodies))

    def test_group_audience_publishes_the_report(self):
        a, b = self.agents()
        task = self.finished(
            b,
            json.dumps({"reply": "PUBLIC RESULT"}),
            private=True,
            reply_to=a,
            audience="group",
        )
        Coordinator(self.store, self.backend).process_completed(task)
        bodies = [m["body"] for m in self.store.project_view(self.other["id"], self.project)["messages"]]
        self.assertIn("PUBLIC RESULT", bodies)

    def test_owner_audience_reports_to_the_executing_owner(self):
        a, b = self.agents()
        task = self.finished(
            b,
            json.dumps({"reply": "OWNER REPORT"}),
            private=True,
            reply_to=a,
            audience="owner",
        )
        Coordinator(self.store, self.backend).process_completed(task)
        own = self.store.project_view(self.owner["id"], self.project)["messages"]
        self.assertEqual(own[-1]["recipient_id"], self.owner["id"])
        self.assertEqual(own[-1]["body"], "OWNER REPORT")
        outsider = [m["body"] for m in self.store.project_view(self.other["id"], self.project)["messages"]]
        self.assertNotIn("OWNER REPORT", outsider)

    def test_group_audience_allows_one_group_message_and_no_more(self):
        a, b = self.agents()
        report = json.dumps(
            {
                "reply": "RESULT",
                "messages": [
                    {"recipient": None, "body": "ONE"},
                    {"recipient": None, "body": "TWO"},
                    {"recipient": b, "body": "SELF NOTE"},
                ],
            }
        )
        task = self.finished(b, report, private=True, reply_to=a, audience="group")
        Coordinator(self.store, self.backend).process_completed(task)
        bodies = [m["body"] for m in self.store.project_view(self.other["id"], self.project)["messages"]]
        self.assertIn("RESULT", bodies)
        self.assertIn("ONE", bodies)
        self.assertNotIn("TWO", bodies)

    def test_delegating_to_another_persons_agent_notifies_its_owner(self):
        a, _ = self.agents()
        from agent_harness.broker import PullBackend

        PullBackend(self.store).create_pair(self.other["id"], "lab2")
        self.backend.projects["backend-c"] = {"node": "lab2"}
        borrowed = self.store.add_agent(
            self.other["id"],
            {
                "project": self.project,
                "name": "外来员工",
                "node": "lab2",
                "backend_project": "backend-c",
                "model": "gpt-test",
                "effort": "low",
                "timeout": 30,
            },
            self.backend,
        )["id"]
        delegation = {
            "agent": borrowed,
            "title": "借用机器",
            "instructions": "跑一次实验",
            "stage": "discuss",
            "allowed_files": "只读",
            "acceptance": "结果",
            "validation": "复核",
        }
        task = self.finished(a, json.dumps({"reply": "已安排", "tasks": [delegation]}))
        Coordinator(self.store, self.backend).process_completed(task)
        notices = [
            m
            for m in self.store.project_view(self.other["id"], self.project)["messages"]
            if m["recipient_id"] == self.other["id"] and m["sender_id"] == borrowed
        ]
        self.assertTrue(notices)
        self.assertIn("借用机器", notices[0]["body"])
        # The owner is told a task exists, never what it says.
        self.assertNotIn("跑一次实验", notices[0]["body"])

    def test_human_private_reply_stays_private_and_third_agent_cannot_read(self):
        a, b = self.agents()
        self.store.message(self.owner["id"], self.project, "秘密问题", a)
        c = Coordinator(self.store, self.backend)
        c.wake_messages()
        c.dispatch()
        c.collect()
        visible = self.store.visible_messages(b)
        self.assertEqual(visible, [])
        audit = self.store.project_view(self.other["id"], self.project)["messages"]
        self.assertEqual(audit, [])
        own = self.store.project_view(self.owner["id"], self.project)["messages"]
        self.assertEqual(own[-1]["recipient_id"], self.owner["id"])
        self.assertEqual(own[-1]["body"], "已完成")

    def test_failure_notice_does_not_relaunch_model(self):
        a, _ = self.agents()
        task = self.finished(a, "failed report", "failed")
        c = Coordinator(self.store, self.backend)
        c.process_completed(task)
        c.tick()
        self.assertEqual(self.backend.submissions, [])
        bodies = [m["body"] for m in self.store.project_view(self.owner["id"], self.project)["messages"]]
        self.assertTrue(any("未能回复" in body and "failed" in body for body in bodies))

    def test_terminal_error_keeps_completed_reply_without_running_actions(self):
        a, b = self.agents()
        report = json.dumps(
            {
                "reply": "已经生成的完整回复",
                "messages": [],
                "tasks": [{"agent": b, "title": "不应执行"}],
                "executions": [{"template": "not-run"}],
                "memory_notes": ["不应保存"],
            }
        )
        task = self.finished(a, report, "timed_out")
        Coordinator(self.store, self.backend).process_completed(task)
        view = self.store.project_view(self.owner["id"], self.project)
        self.assertIn("已经生成的完整回复", [m["body"] for m in view["messages"]])
        self.assertTrue(any("timed_out" in m["body"] and "未执行派工" in m["body"] for m in view["messages"]))
        with self.store.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM tasks WHERE id<>?", (task,)).fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM executions").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM project_memory_notes").fetchone())

    def test_private_plain_report_does_not_fall_back_to_group(self):
        a, b = self.agents()
        task = self.finished(a, "private plain text", private=True, reply_to=self.owner["id"])
        Coordinator(self.store, self.backend).process_completed(task)
        self.assertEqual(self.store.visible_messages(b), [])
        self.assertEqual(
            self.store.project_view(self.owner["id"], self.project)["messages"][-1]["recipient_id"],
            self.owner["id"],
        )

    def test_private_structured_turn_cannot_broadcast(self):
        a, b = self.agents()
        report = json.dumps(
            {
                "reply": "private reply",
                "messages": [
                    {"recipient": None, "body": "forbidden leak"},
                    {"recipient": b, "body": "third-party leak"},
                ],
            }
        )
        task = self.finished(a, report, private=True, reply_to=self.owner["id"])
        Coordinator(self.store, self.backend).process_completed(task)
        self.assertEqual(self.store.visible_messages(b), [])
        bodies = [m["body"] for m in self.store.project_view(self.owner["id"], self.project)["messages"]]
        self.assertEqual(bodies, ["private reply"])

    def test_public_context_excludes_private_history(self):
        a, _ = self.agents()
        self.store.message(self.owner["id"], self.project, "private marker", a)
        self.store.message(self.owner["id"], self.project, "public question")
        task = self.finished(a, "{}")
        with self.store.connect() as db:
            agent = dict(db.execute("SELECT * FROM agents WHERE id=?", (a,)).fetchone())
            row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())
        context = Coordinator(self.store, self.backend).agent_context(agent, row)
        self.assertNotIn("private marker", context)
        self.assertIn("public question", context)

    def test_private_and_public_messages_have_independent_cursors(self):
        a, _ = self.agents()
        self.store.message(self.owner["id"], self.project, "private first", a)
        self.store.message(self.owner["id"], self.project, "public second")
        c = Coordinator(self.store, self.backend)
        c.wake_messages()
        c.dispatch()
        c.collect()
        self.assertEqual(len(self.backend.submissions), 1)
        self.assertIn("private first", self.backend.submissions[0][2])
        c.wake_messages()
        c.dispatch()
        c.collect()
        self.assertEqual(len(self.backend.submissions), 2)
        self.assertIn("public second", self.backend.submissions[1][2])
        self.assertNotIn("private first", self.backend.submissions[1][2])
        c.tick()
        self.assertEqual(len(self.backend.submissions), 2)

    def test_peer_cannot_delegate_and_admin_private_assignment_is_scoped(self):
        a, b = self.agents()
        assignment = {
            "agent": a,
            "title": "派活",
            "instructions": "讨论",
            "stage": "discuss",
            "allowed_files": "只读",
            "acceptance": "结论",
            "validation": "复核",
        }
        task = self.finished(b, json.dumps({"reply": "建议", "tasks": [assignment]}))
        c = Coordinator(self.store, self.backend)
        c.process_completed(task)
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM tasks WHERE state='queued'").fetchone()[0],
                0,
            )
        assignment["agent"] = b
        task = self.finished(a, json.dumps({"reply": "安排工作", "tasks": [assignment]}))
        c.process_completed(task)
        with self.store.connect() as db:
            row = db.execute("SELECT contract FROM tasks WHERE state='queued'").fetchone()
            self.assertTrue(json.loads(row["contract"])["private_turn"])

    def test_rejected_assignment_does_not_send_false_dispatch_notice(self):
        a, b = self.agents()
        self.finished(b, "", "running")
        assignment = {
            "agent": b,
            "title": "busy target",
            "instructions": "discuss",
            "stage": "discuss",
            "allowed_files": "read",
            "acceptance": "reply",
            "validation": "check",
        }
        task = self.finished(a, json.dumps({"reply": "", "tasks": [assignment]}))
        Coordinator(self.store, self.backend).process_completed(task)
        self.assertFalse(any(m["body"].startswith("分派任务") for m in self.store.visible_messages(b)))

    def test_start_crash_is_unknown_and_never_resubmitted(self):
        a, _ = self.agents()
        task = self.finished(a, "", "starting")
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET job=? WHERE id=?", (uid("job"), task))
        c = Coordinator(self.store, self.backend)
        c.recover()
        c.dispatch()
        self.assertEqual(self.backend.submissions, [])
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT state FROM tasks WHERE id=?", (task,)).fetchone()[0],
                "unknown",
            )

    def test_cross_owner_cannot_manually_schedule_other_agent(self):
        a, _ = self.agents()
        with self.assertRaises(PermissionError):
            self.store.task(
                self.other["id"],
                {
                    "project": self.project,
                    "agent": a,
                    "title": "unwanted work",
                    "instructions": "run",
                    "stage": "discuss",
                    "allowed_files": "read",
                    "acceptance": "reply",
                    "validation": "check",
                },
            )

    def test_configuration_uncertainty_blocks_new_tasks(self):
        a, _ = self.agents()

        def failure(*args):
            raise RuntimeError("network failed after request")

        self.backend.configure = failure
        with self.assertRaises(RuntimeError):
            self.store.configure_agent(
                self.owner["id"],
                {
                    "agent": a,
                    "model": "test",
                    "effort": "high",
                    "timeout": 45,
                    "enabled": True,
                },
                self.backend,
            )
        with self.assertRaises(ValueError):
            self.finished(a, "{}")


if __name__ == "__main__":
    unittest.main()
