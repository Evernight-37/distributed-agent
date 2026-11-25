import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.app_bridge import AppBridge
from agent_harness.appserver_client import PublicReplyDecoder, application_version
from agent_harness.collaboration_store import Store
from agent_harness.coordinator import Coordinator
from tests.helpers import provision_account


class Backend:
    def __init__(self):
        self.result = {"status": "running"}
        self.submissions = []

    def submit(self, project, context, stage, scope, acceptance, validation):
        self.submissions.append(json.loads(context))
        return {"job": "job-" + str(len(self.submissions))}

    def status(self, job, fetch=True):
        return self.result


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.owner = provision_account(self.store, "owner", bootstrap=True)["id"]
        self.project = self.store.project_create(self.owner, "project")["id"]
        self.backend = Backend()
        with self.store.connect() as db:
            db.execute("INSERT INTO nodes(name,owner_id) VALUES('node',?)", (self.owner,))
            for agent, name in (("admin", "Admin"), ("peer", "Peer")):
                db.execute(
                    "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout,enabled) VALUES(?,?,?,?,?,?,?,?,?,1)",
                    (
                        agent,
                        self.project,
                        name,
                        self.owner,
                        "node",
                        agent,
                        "model",
                        "low",
                        30,
                    ),
                )
        self.store.set_admin(self.owner, self.project, "admin")
        self.coordinator = Coordinator(self.store, self.backend)

    def tearDown(self):
        self.tmp.cleanup()

    def test_app_server_reports_release_version(self):
        expected = Path(__file__).resolve().parents[1].joinpath("VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(application_version(), expected)

    def test_administrator_self_assignment_reports_back_to_the_group(self):
        self.store.message(self.owner, self.project, "请修改 README.md 中的项目说明", mention_agent_id="admin")
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.backend.result = {
            "status": "needs_review",
            "report": json.dumps(
                {
                    "reply": "开始修改项目说明",
                    "tasks": [
                        {
                            "agent": "admin",
                            "title": "修改说明",
                            "instructions": "按人类要求修改 README.md",
                            "stage": "implement",
                            "allowed_files": "README.md",
                            "acceptance": "说明已更新",
                            "validation": "核对差异",
                        }
                    ],
                    "messages": [],
                    "executions": [],
                    "done": True,
                }
            ),
        }
        self.coordinator.collect()
        with self.store.connect() as db:
            task = dict(db.execute("SELECT * FROM tasks WHERE state='queued'").fetchone())
        contract = json.loads(task["contract"])
        self.assertEqual(contract["stage"], "implement")
        self.assertFalse(contract["private_turn"])
        self.assertIsNone(contract["reply_to"])
        self.coordinator.dispatch()
        self.backend.result = {
            "status": "needs_review",
            "report": json.dumps({"reply": "项目说明已更新", "done": True}),
        }
        self.coordinator.collect()
        view = self.store.project_view(self.owner, self.project)
        self.assertEqual(view["messages"][-1]["body"], "项目说明已更新")
        self.assertEqual(view["messages"][-1]["conversation_id"], "group:" + self.project)
        self.assertIsNone(view["messages"][-1]["recipient_id"])

    def test_reply_stream_stays_above_midturn_human_message(self):
        self.store.message(self.owner, self.project, "first", mention_agent_id="admin")
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.store.message(self.owner, self.project, "second")
        self.backend.result = {"status": "running", "reply_draft": "partial"}
        self.coordinator.collect()
        view = self.store.project_view(self.owner, self.project)
        shown = [m for m in view["messages"] if m["sender_type"] != "system"]
        self.assertEqual([m["body"] for m in shown], ["first", "partial", "second"])
        draft_id = shown[1]["id"]
        self.assertEqual(
            [m["body"] for m in self.store.visible_messages("peer") if m["sender_type"] != "system"],
            ["first", "second"],
        )
        self.backend.result = {
            "status": "needs_review",
            "report": json.dumps(
                {
                    "reply": "finished",
                    "done": True,
                    "messages": [],
                    "tasks": [],
                    "executions": [],
                }
            ),
        }
        self.coordinator.collect()
        view = self.store.project_view(self.owner, self.project)
        shown = [m for m in view["messages"] if m["sender_type"] != "system"]
        self.assertEqual([m["body"] for m in shown], ["first", "finished", "second"])
        self.assertEqual(shown[1]["id"], draft_id)

    def test_agent_can_finish_without_a_reply(self):
        self.store.message(self.owner, self.project, "notice", mention_agent_id="admin")
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.backend.result = {
            "status": "needs_review",
            "report": json.dumps(
                {
                    "reply": "",
                    "done": True,
                    "messages": [],
                    "tasks": [],
                    "executions": [],
                }
            ),
        }
        self.coordinator.collect()
        self.assertEqual(
            [
                m["body"]
                for m in self.store.project_view(self.owner, self.project)["messages"]
                if m["sender_type"] != "system"
            ],
            ["notice"],
        )
        self.coordinator.wake_messages()
        self.assertEqual(len(self.backend.submissions), 1)

    def test_private_stream_remains_addressed_to_sender(self):
        self.store.message(self.owner, self.project, "private question", recipient="peer")
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.backend.result = {"status": "running", "reply_draft": "private partial"}
        self.coordinator.collect()
        view = self.store.project_view(self.owner, self.project)
        partial = next(m for m in view["messages"] if m["body"] == "private partial")
        self.assertEqual(partial["recipient_id"], self.owner)
        self.assertNotIn("private partial", [m["body"] for m in self.store.visible_messages("admin")])

    def test_public_mention_routes_to_only_named_agent(self):
        self.store.message(self.owner, self.project, "Please review", mention_agent_id="peer")
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            tasks = [dict(r) for r in db.execute("SELECT agent_id FROM tasks")]
        self.assertEqual([t["agent_id"] for t in tasks], ["peer"])
        message = next(
            m for m in self.store.project_view(self.owner, self.project)["messages"] if m["body"] == "Please review"
        )
        self.assertEqual(message["mention_agent_name"], "Peer")
        with self.assertRaises(ValueError):
            self.store.message(self.owner, self.project, "bad", mention_agent_id="missing")
        with self.assertRaises(ValueError):
            self.store.message(
                self.owner,
                self.project,
                "bad",
                recipient="admin",
                mention_agent_id="peer",
            )

    def test_other_project_member_can_call_an_agent_in_the_public_group(self):
        other = provision_account(
            self.store,
            "other-member",
            invite=self.store.invite(self.owner, self.project)["invite"],
        )["id"]
        self.store.message(
            other,
            self.project,
            "Please answer my public question",
            mention_agent_id="peer",
        )
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            self.assertEqual(
                [r["agent_id"] for r in db.execute("SELECT agent_id FROM tasks")],
                ["peer"],
            )
        self.coordinator.dispatch()
        context = self.backend.submissions[0]
        self.assertIn(
            "Please answer my public question",
            [m["body"] for m in context["visible_messages"]],
        )

    def test_unaddressed_public_message_from_other_member_does_not_wake_administrator(self):
        other = provision_account(
            self.store,
            "other-member",
            invite=self.store.invite(self.owner, self.project)["invite"],
        )["id"]
        self.store.message(other, self.project, "A public question for the administrator")
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            self.assertEqual([r["agent_id"] for r in db.execute("SELECT agent_id FROM tasks")], [])

    def test_group_chat_is_context_and_mentions_create_independent_turns(self):
        self.store.message(self.owner, self.project, "general update")
        first = self.store.message(self.owner, self.project, "please answer", mention_agent_id="peer")
        second = self.store.message(self.owner, self.project, "second request", mention_agent_id="peer")
        self.coordinator.wake_messages()
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.assertEqual(len(self.backend.submissions), 1)
        first_turn = self.backend.submissions[0]
        self.assertEqual(first_turn["identity"]["id"], "peer")
        self.assertEqual(first_turn["task"]["source_message_id"], first["id"])
        self.assertNotIn("second request", [message["body"] for message in first_turn["visible_messages"]])
        self.assertIn("general update", [message["body"] for message in first_turn["visible_messages"]])
        self.backend.result = {"status": "needs_review", "report": json.dumps({"reply": "only peer replies"})}
        self.coordinator.collect()
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.assertEqual(len(self.backend.submissions), 2)
        second_turn = self.backend.submissions[1]
        self.assertEqual(second_turn["identity"]["id"], "peer")
        self.assertEqual(second_turn["task"]["source_message_id"], second["id"])

    def test_independent_formal_task_does_not_consume_pending_mention(self):
        mention = self.store.message(self.owner, self.project, "pending mention", mention_agent_id="peer")
        self.store.task(
            self.owner,
            {
                "project": self.project,
                "agent": "peer",
                "title": "independent assignment",
                "instructions": "finish this task",
                "acceptance": "answer",
                "validation": "check",
            },
        )
        self.coordinator.dispatch()
        self.backend.result = {"status": "needs_review", "report": json.dumps({"reply": "formal result"})}
        self.coordinator.collect()
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.assertEqual(len(self.backend.submissions), 2)
        self.assertEqual(self.backend.submissions[1]["task"]["source_message_id"], mention["id"])

    def test_delegation_preserves_pending_private_message_and_routes_public_result(self):
        request = self.store.message(self.owner, self.project, "Please delegate", mention_agent_id="admin")
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        with self.store.connect() as db:
            private = self.store._message(db, self.project, "admin", "agent", "Separate question", "peer")
        self.backend.result = {
            "status": "needs_review",
            "report": json.dumps(
                {
                    "tasks": [
                        {
                            "agent": "peer",
                            "title": "Deliverable",
                            "instructions": "Complete the assignment",
                            "acceptance": "Deliver the result",
                            "validation": "Check it",
                            "audience": "group",
                        }
                    ]
                }
            ),
        }
        self.coordinator.collect()
        self.coordinator.dispatch()
        with self.store.connect() as db:
            child = db.execute("SELECT * FROM task_trace WHERE parent_task_id IS NOT NULL").fetchone()
            parent = db.execute("SELECT * FROM task_trace WHERE task_id=?", (child["parent_task_id"],)).fetchone()
            self.assertEqual(parent["source_message_id"], request["id"])
            self.assertGreater(child["source_message_id"], private["id"])
            self.assertEqual(child["delivery_conversation_id"], self.store.group_conversation(self.project))
        self.backend.result = {"status": "needs_review", "report": json.dumps({"reply": "Public result"})}
        self.coordinator.collect()
        view = self.store.project_view(self.owner, self.project)
        public = next(m for m in view["messages"] if m["body"] == "Public result")
        self.assertIsNone(public["recipient_id"])
        self.assertEqual(public["task_id"], child["task_id"])
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        turns = {turn["identity"]["id"]: turn for turn in self.backend.submissions[2:]}
        self.assertEqual(turns["peer"]["task"]["source_message_id"], private["id"])
        admin = turns["admin"]
        self.assertTrue(admin["task"]["private_turn"])
        note = next(m for m in admin["visible_messages"] if m["id"] == admin["task"]["source_message_id"])
        self.assertIn(f"消息 #{public['id']}", note["body"])
        self.backend.result = {"status": "needs_review", "report": json.dumps({"reply": ""})}
        self.coordinator.collect()
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.assertEqual(len(self.backend.submissions), 4)

    def test_plain_at_text_is_not_a_mention(self):
        self.store.message(self.owner, self.project, "@Peer please review")
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            tasks = [r["agent_id"] for r in db.execute("SELECT agent_id FROM tasks")]
        self.assertEqual(tasks, [])

    def test_signed_message_path_preserves_explicit_mention(self):
        self.store.message(
            self.owner,
            self.project,
            "Please review",
            mention_agent_id="peer",
        )
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            self.assertEqual(
                [r["agent_id"] for r in db.execute("SELECT agent_id FROM tasks")],
                ["peer"],
            )

    def test_desktop_refuses_message_when_team_service_is_too_old(self):
        bridge = AppBridge(Path(self.tmp.name) / "app")
        bridge.identity = {"public_key": "unused"}
        bridge.profile = {"server": "http://127.0.0.1:8765"}
        with patch.object(bridge, "_post", return_value={"nonce": "nonce"}) as post:
            with self.assertRaisesRegex(RuntimeError, "版本过旧"):
                bridge._rpc("message", {"project": self.project, "body": "hello"})
        self.assertEqual(post.call_count, 1)

    def test_public_reply_decoder_never_emits_private_fields(self):
        decoder = PublicReplyDecoder()
        pieces = [
            '{"reply":"hello',
            ' world","messages":[{"body":"SECRET"}],',
            '"tasks":[]}',
        ]
        self.assertEqual(
            [decoder.feed(part) for part in pieces],
            ["hello", "hello world", "hello world"],
        )
        self.assertNotIn("SECRET", decoder.feed(""))
        self.assertEqual(
            PublicReplyDecoder().feed('{"messages":[{"body":"SECRET"}],"reply":"late"}'),
            "",
        )


if __name__ == "__main__":
    unittest.main()
