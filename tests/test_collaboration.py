import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_harness.broker import PullBackend
from agent_harness.collaboration_store import Store
from agent_harness.coordinator import Coordinator
from tests.helpers import provision_account


class FakeBackend:
    def __init__(self, node="lab"):
        self.node = node
        self.configured = []
        self.submissions = []
        self.projects = {}
        self.report = None

    def project(self, name):
        return self.projects[name]

    def configure(self, project, model, effort, timeout):
        self.configured.append((project, model, effort, timeout))

    def submit(self, project, context, stage, allowed, acceptance, validation):
        job = "job-test-%d" % (len(self.submissions) + 1)
        self.submissions.append((job, project, context, stage))
        return {"job": job}

    def status(self, job, fetch=True):
        return {
            "status": "needs_review",
            "report": self.report or json.dumps({"reply": "已完成"}),
        }


class CollaborationTests(unittest.TestCase):
    def tearDown(self):
        self.tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.owner = provision_account(self.store, "owner", bootstrap=True)
        self.other = None
        self.project = self.store.project_create(self.owner["id"], "研究项目")["id"]
        token = self.store.invite(self.owner["id"], self.project)["invite"]
        # Invitations are consumed by the signed-account test fixture, which creates membership.
        self.other = provision_account(self.store, "peer", invite=token)
        PullBackend(self.store).create_pair(self.owner["id"], "lab")
        self.backend = FakeBackend()
        self.backend.projects["backend-a"] = {"node": "lab"}
        self.backend.projects["backend-b"] = {"node": "lab"}

    def add_agent(self, user, backend_project, name):
        return self.store.add_agent(
            user,
            {
                "project": self.project,
                "name": name,
                "node": "lab",
                "backend_project": backend_project,
                "model": "gpt-test",
                "effort": "low",
                "timeout": 30,
            },
            self.backend,
        )["id"]

    def test_invitation_and_owner_only_configuration(self):
        agent = self.add_agent(self.owner["id"], "backend-a", "主控")
        with self.assertRaises(PermissionError):
            self.store.configure_agent(
                self.other["id"],
                {
                    "agent": agent,
                    "model": "bad",
                    "effort": "high",
                    "timeout": 1,
                    "enabled": True,
                },
                self.backend,
            )
        self.store.configure_agent(
            self.owner["id"],
            {
                "agent": agent,
                "model": "gpt-new",
                "effort": "high",
                "timeout": 45,
                "enabled": True,
            },
            self.backend,
        )
        self.assertEqual(self.backend.configured[-1], ("backend-a", "gpt-new", "high", 45))

    def test_private_message_is_visible_only_to_human_participant(self):
        first = self.add_agent(self.owner["id"], "backend-a", "一号")
        second = self.add_agent(self.owner["id"], "backend-b", "二号")
        self.store.message(self.owner["id"], self.project, "给一号的秘密", first)
        self.assertEqual([m["body"] for m in self.store.visible_messages(first)], ["给一号的秘密"])
        self.assertEqual(self.store.visible_messages(second), [])
        audit = self.store.project_view(self.other["id"], self.project)
        self.assertEqual(audit["messages"], [])
        own = self.store.project_view(self.owner["id"], self.project)
        self.assertEqual(own["messages"][0]["body"], "给一号的秘密")
        self.assertEqual(own["messages"][0]["channel"], "direct")

    def test_one_active_task_is_enforced(self):
        agent = self.add_agent(self.owner["id"], "backend-a", "执行者")
        data = {
            "project": self.project,
            "agent": agent,
            "title": "检查",
            "instructions": "检查文件",
            "stage": "discuss",
            "allowed_files": "只读",
            "acceptance": "有结论",
            "validation": "复核",
        }
        self.store.task(self.owner["id"], data)
        # The database uniqueness guard is the final race-safe enforcement;
        # current API surfaces its IntegrityError rather than normalizing it.
        with self.assertRaises((ValueError, sqlite3.IntegrityError)):
            self.store.task(self.owner["id"], data)

    def test_coordinator_dispatches_short_turn_and_processes_reply(self):
        agent = self.add_agent(self.owner["id"], "backend-a", "执行者")
        self.store.set_admin(self.owner["id"], self.project, agent)
        task = self.store.task(
            self.owner["id"],
            {
                "project": self.project,
                "agent": agent,
                "title": "讨论",
                "instructions": "给出结论",
                "stage": "discuss",
                "allowed_files": "只读",
                "acceptance": "回复",
                "validation": "检查",
            },
        )["id"]
        coordinator = Coordinator(self.store, self.backend)
        coordinator.dispatch()
        self.assertEqual(len(self.backend.submissions), 1)
        coordinator.collect()
        view = self.store.project_view(self.owner["id"], self.project)
        self.assertTrue(any(m["body"] == "已完成" for m in view["messages"]))
        self.assertEqual(view["tasks"][0]["id"], task)

    def test_idle_coordinator_does_not_submit_model_turn(self):
        self.add_agent(self.owner["id"], "backend-a", "空闲")
        Coordinator(self.store, self.backend).tick()
        self.assertEqual(self.backend.submissions, [])

    def test_checkpoint_is_not_posted_before_actual_reply(self):
        agent = self.add_agent(self.owner["id"], "backend-a", "执行者")
        self.store.set_admin(self.owner["id"], self.project, agent)
        self.store.task(
            self.owner["id"],
            {
                "project": self.project,
                "agent": agent,
                "title": "讨论",
                "instructions": "回复",
                "stage": "discuss",
                "allowed_files": "只读",
                "acceptance": "回复",
                "validation": "检查",
            },
        )
        self.backend.report = json.dumps(
            {
                "reply": "[CHECKPOINT] 正在整理上下文",
                "messages": [{"recipient": None, "body": "这是最终回复"}],
            }
        )
        coordinator = Coordinator(self.store, self.backend)
        coordinator.dispatch()
        coordinator.collect()
        bodies = [m["body"] for m in self.store.project_view(self.owner["id"], self.project)["messages"]]
        self.assertIn("这是最终回复", bodies)
        self.assertFalse(any(body.startswith("[CHECKPOINT]") for body in bodies))


if __name__ == "__main__":
    unittest.main()
