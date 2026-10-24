import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_bridge import AppBridge
from appserver_client import PublicReplyDecoder, application_version
from collaboration_store import Store
from coordinator import Coordinator
from panel import action as panel_action


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
        self.owner = self.store.signup("owner", "password", bootstrap=True)["id"]
        self.project = self.store.project_create(self.owner, "project")["id"]
        self.backend = Backend()
        with self.store.connect() as db:
            db.execute("INSERT INTO nodes(name,owner_id) VALUES('node',?)", (self.owner,))
            for agent, name in (("admin", "Admin"), ("peer", "Peer")):
                db.execute(
                    "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout,enabled) VALUES(?,?,?,?,?,?,?,?,?,1)",
                    (agent, self.project, name, self.owner, "node", agent, "model", "low", 30),
                )
        self.store.set_admin(self.owner, self.project, "admin")
        self.coordinator = Coordinator(self.store, self.backend)

    def tearDown(self):
        self.tmp.cleanup()

    def test_app_server_reports_release_version(self):
        expected = Path(__file__).resolve().parents[1].joinpath("VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(application_version(), expected)

    def test_administrator_self_assignment_reports_back_to_the_group(self):
        self.store.message(self.owner, self.project, "请修改 README.md 中的项目说明")
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
        self.store.message(self.owner, self.project, "first")
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.store.message(self.owner, self.project, "second")
        self.backend.result = {"status": "running", "reply_draft": "partial"}
        self.coordinator.collect()
        view = self.store.project_view(self.owner, self.project)
        self.assertEqual([m["body"] for m in view["messages"]], ["first", "partial", "second"])
        draft_id = view["messages"][1]["id"]
        self.assertEqual([m["body"] for m in self.store.visible_messages("peer")], ["first", "second"])
        self.backend.result = {
            "status": "needs_review",
            "report": json.dumps({"reply": "finished", "done": True, "messages": [], "tasks": [], "executions": []}),
        }
        self.coordinator.collect()
        view = self.store.project_view(self.owner, self.project)
        self.assertEqual([m["body"] for m in view["messages"]], ["first", "finished", "second"])
        self.assertEqual(view["messages"][1]["id"], draft_id)

    def test_agent_can_finish_without_a_reply(self):
        self.store.message(self.owner, self.project, "notice")
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.backend.result = {
            "status": "needs_review",
            "report": json.dumps({"reply": "", "done": True, "messages": [], "tasks": [], "executions": []}),
        }
        self.coordinator.collect()
        self.assertEqual([m["body"] for m in self.store.project_view(self.owner, self.project)["messages"]], ["notice"])
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
        message = self.store.project_view(self.owner, self.project)["messages"][0]
        self.assertEqual(message["mention_agent_name"], "Peer")
        with self.assertRaises(ValueError):
            self.store.message(self.owner, self.project, "bad", mention_agent_id="missing")
        with self.assertRaises(ValueError):
            self.store.message(self.owner, self.project, "bad", recipient="admin", mention_agent_id="peer")

    def test_other_project_member_can_call_an_agent_in_the_public_group(self):
        other = self.store.signup(
            "other-member", "password", invite=self.store.invite(self.owner, self.project)["invite"]
        )["id"]
        self.store.message(other, self.project, "Please answer my public question", mention_agent_id="peer")
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            self.assertEqual([r["agent_id"] for r in db.execute("SELECT agent_id FROM tasks")], ["peer"])
        self.coordinator.dispatch()
        context = self.backend.submissions[0]
        self.assertIn("Please answer my public question", [m["body"] for m in context["visible_messages"]])

    def test_other_member_public_message_wakes_the_administrator(self):
        other = self.store.signup(
            "other-member", "password", invite=self.store.invite(self.owner, self.project)["invite"]
        )["id"]
        self.store.message(other, self.project, "A public question for the administrator")
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            self.assertEqual([r["agent_id"] for r in db.execute("SELECT agent_id FROM tasks")], ["admin"])

    def test_mentions_and_group_chat_create_independent_turns(self):
        self.store.message(self.owner, self.project, "general update")
        self.store.message(self.owner, self.project, "please answer", mention_agent_id="peer")
        self.coordinator.wake_messages()
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            self.assertEqual([r["agent_id"] for r in db.execute("SELECT agent_id FROM tasks")], ["peer", "admin"])

    def test_plain_at_text_is_not_a_mention(self):
        self.store.message(self.owner, self.project, "@Peer please review")
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            tasks = [r["agent_id"] for r in db.execute("SELECT agent_id FROM tasks")]
        self.assertEqual(tasks, ["admin"])

    def test_panel_action_preserves_explicit_mention(self):
        panel_action(
            self.store,
            {"id": self.owner},
            {"action": "message", "project": self.project, "body": "Please review", "mention_agent_id": "peer"},
            self.backend,
        )
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            self.assertEqual([r["agent_id"] for r in db.execute("SELECT agent_id FROM tasks")], ["peer"])

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
        pieces = ['{"reply":"hello', ' world","messages":[{"body":"SECRET"}],', '"tasks":[]}']
        self.assertEqual([decoder.feed(part) for part in pieces], ["hello", "hello world", "hello world"])
        self.assertNotIn("SECRET", decoder.feed(""))
        self.assertEqual(PublicReplyDecoder().feed('{"messages":[{"body":"SECRET"}],"reply":"late"}'), "")


if __name__ == "__main__":
    unittest.main()
