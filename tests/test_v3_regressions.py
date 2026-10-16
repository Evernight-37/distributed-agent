import json
import unittest
from collaboration_store import uid
from coordinator import Coordinator
from panel import action
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

    def finished(self, agent, report, state="needs_review", private=False, reply_to=None):
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
        }
        task = self.store.task(self.owner["id"], data)["id"]
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET state=?,report=? WHERE id=?", (state, report, task))
        return task

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
            self.store.project_view(self.owner["id"], self.project)["messages"][-1]["recipient_id"], self.owner["id"]
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
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks WHERE state='queued'").fetchone()[0], 0)
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
            self.assertEqual(db.execute("SELECT state FROM tasks WHERE id=?", (task,)).fetchone()[0], "unknown")

    def test_cross_owner_cannot_approve_execution(self):
        a, _ = self.agents()
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO executions(id,project_id,agent_id,template,state,created_at) VALUES('exec',?,?,?,'awaiting_owner',0)",
                (self.project, a, "train"),
            )
        with self.assertRaises(PermissionError):
            action(self.store, self.other, {"action": "execution-approve", "execution": "exec"}, self.backend)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM executions WHERE id='exec'").fetchone()[0], "awaiting_owner")

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

    def test_cancellation_is_durable_during_startup(self):
        a, _ = self.agents()
        task = self.finished(a, "", "starting")
        result = action(self.store, self.owner, {"action": "cancel-task", "task": task}, self.backend)
        self.assertEqual(result["status"], "cancellation_requested")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT cancel_requested FROM tasks WHERE id=?", (task,)).fetchone()[0], 1)

    def test_configuration_uncertainty_blocks_new_tasks(self):
        a, _ = self.agents()

        def failure(*args):
            raise RuntimeError("network failed after request")

        self.backend.configure = failure
        with self.assertRaises(RuntimeError):
            self.store.configure_agent(
                self.owner["id"],
                {"agent": a, "model": "test", "effort": "high", "timeout": 45, "enabled": True},
                self.backend,
            )
        with self.assertRaises(ValueError):
            self.finished(a, "{}")

    def test_pause_prevents_new_calls(self):
        self.agents()
        action(
            self.store, self.owner, {"action": "pause-project", "project": self.project, "paused": True}, self.backend
        )
        self.store.message(self.owner["id"], self.project, "new message")
        Coordinator(self.store, self.backend).tick()
        self.assertEqual(self.backend.submissions, [])


if __name__ == "__main__":
    unittest.main()
