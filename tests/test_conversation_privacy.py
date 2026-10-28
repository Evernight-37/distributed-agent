"""Server-side conversation authorization."""

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from agent_harness.app_bridge import AppBridge
from agent_harness.broker import PullBackend
from agent_harness.collaboration_store import Store
from agent_harness.coordinator import Coordinator
from agent_harness.panel import handler_for
from tests import test_collaboration as fixtures
from tests.helpers import provision_account


class ConversationPrivacyTests(unittest.TestCase):
    setUp = fixtures.CollaborationTests.setUp
    tearDown = fixtures.CollaborationTests.tearDown
    add_agent = fixtures.CollaborationTests.add_agent

    def people_and_agents(self):
        third = provision_account(
            self.store,
            "third",
            invite=self.store.invite(self.owner["id"], self.project)["invite"],
        )
        first = self.add_agent(self.owner["id"], "backend-a", "First")
        second = self.add_agent(self.owner["id"], "backend-b", "Second")
        return third["id"], first, second

    def bodies(self, user):
        return [m["body"] for m in self.store.project_view(user, self.project)["messages"]]

    def test_four_conversation_classes_and_agent_visibility(self):
        third, first, second = self.people_and_agents()
        owner, other = self.owner["id"], self.other["id"]
        self.store.message(owner, self.project, "public")
        ha = self.store.message(owner, self.project, "human agent secret", first)
        hh = self.store.message(owner, self.project, "human human secret", other)
        with self.store.connect() as db:
            aa = self.store._message(db, self.project, first, "agent", "internal agent note", second)
        self.assertEqual(
            self.bodies(owner),
            [
                "public",
                "human agent secret",
                "human human secret",
                "internal agent note",
            ],
        )
        self.assertEqual(self.bodies(other), ["public", "human human secret", "internal agent note"])
        self.assertEqual(self.bodies(third), ["public", "internal agent note"])
        self.assertEqual(
            [m["body"] for m in self.store.visible_messages(second)],
            ["public", "internal agent note"],
        )
        self.assertNotIn(
            "human human secret",
            [m["body"] for m in self.store.visible_messages(first)],
        )
        own = self.store.project_view(owner, self.project)
        self.assertEqual(ha["conversation_id"], "human_agent:" + owner + ":" + first)
        self.assertEqual(hh["conversation_id"], "human_human:" + ":".join(sorted((owner, other))))
        self.assertEqual(aa["conversation_id"], "agent_agent:" + ":".join(sorted((first, second))))
        self.assertEqual(
            {c["kind"] for c in own["conversations"]},
            {"group", "human_agent", "human_human", "agent_agent"},
        )
        self.assertEqual(
            {c["kind"] for c in self.store.project_view(third, self.project)["conversations"]},
            {"group", "agent_agent"},
        )

    def test_conversation_id_cannot_override_recipient_or_impersonate_agent(self):
        _, first, second = self.people_and_agents()
        owner, other = self.owner["id"], self.other["id"]
        private = self.store.message(owner, self.project, "start", first)["conversation_id"]
        self.store.message(owner, self.project, "followup", conversation_id=private)
        with self.assertRaises(PermissionError):
            self.store.message(other, self.project, "intrusion", conversation_id=private)
        with self.assertRaises(ValueError):
            self.store.message(
                owner,
                self.project,
                "mismatch",
                recipient=second,
                conversation_id=private,
            )
        with self.assertRaises(PermissionError):
            self.store.message(
                owner,
                self.project,
                "impersonation",
                conversation_id="agent_agent:" + ":".join(sorted((first, second))),
            )
        with self.assertRaises(ValueError):
            self.store.message(owner, self.project, "self", recipient=owner)

    def test_human_can_only_private_message_agents_they_own(self):
        own = self.add_agent(self.owner["id"], "backend-a", "Own")
        PullBackend(self.store).create_pair(self.other["id"], "peer")
        self.backend.projects["backend-b"] = {"node": "peer"}
        foreign = self.store.add_agent(
            self.other["id"],
            {
                "project": self.project,
                "name": "Foreign",
                "node": "peer",
                "backend_project": "backend-b",
                "model": "gpt-test",
                "effort": "low",
                "timeout": 30,
            },
            self.backend,
        )["id"]
        sent = self.store.message(self.owner["id"], self.project, "allowed", own)
        self.assertTrue(sent["conversation_id"].startswith("human_agent:" + self.owner["id"] + ":"))
        with self.assertRaisesRegex(PermissionError, "自己的 Agent"):
            self.store.message(self.owner["id"], self.project, "blocked", foreign)
        forged = "human_agent:" + self.owner["id"] + ":" + foreign
        with self.assertRaises(PermissionError):
            self.store.message(self.owner["id"], self.project, "blocked again", conversation_id=forged)
        # A malformed database row must neither reach nor wake the foreign Agent.
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO messages(project_id,sender_id,sender_type,recipient_id,body,created_at,conversation_id) VALUES(?,?,?,?,?,?,?)",
                (
                    self.project,
                    self.owner["id"],
                    "human",
                    foreign,
                    "forbidden row",
                    1,
                    forged,
                ),
            )
        self.assertNotIn("forbidden row", [m["body"] for m in self.store.visible_messages(foreign)])
        Coordinator(self.store, self.backend).wake_messages()
        with self.store.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM tasks WHERE agent_id=?", (foreign,)).fetchone())

    def test_private_task_contract_and_report_are_not_in_other_humans_view(self):
        _, first, _ = self.people_and_agents()
        owner, other = self.owner["id"], self.other["id"]
        task = self.store.task(
            owner,
            {
                "project": self.project,
                "agent": first,
                "title": "private title",
                "instructions": "PRIVATE CONTRACT",
                "stage": "discuss",
                "allowed_files": "read only",
                "acceptance": "PRIVATE ACCEPTANCE",
                "validation": "check",
                "private_turn": True,
                "reply_to": owner,
            },
        )["id"]
        with self.store.connect() as db:
            db.execute(
                "UPDATE tasks SET report=? WHERE id=?",
                (json.dumps({"reply": "PRIVATE REPORT"}), task),
            )
        other_view = self.store.project_view(other, self.project)
        self.assertEqual(other_view["tasks"], [])
        self.assertEqual(other_view["messages"], [])
        self.assertEqual(len(self.store.project_view(owner, self.project)["tasks"]), 1)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
            self.assertFalse(self.store.task_visible_to_human(db, row, other))

    def test_read_cursor_is_per_conversation_and_authorized(self):
        _, first, _ = self.people_and_agents()
        owner, other = self.owner["id"], self.other["id"]
        cid = self.store.message(owner, self.project, "private", first)["conversation_id"]
        with self.assertRaises(PermissionError):
            self.store.mark_conversation_read(other, self.project, cid)
        own = self.store.project_view(owner, self.project)
        self.assertEqual(next(c for c in own["conversations"] if c["id"] == cid)["unread_count"], 0)
        with self.store.connect() as db:
            self.store._message(db, self.project, first, "agent", "response", owner)
        self.assertEqual(
            next(c for c in self.store.project_view(owner, self.project)["conversations"] if c["id"] == cid)[
                "unread_count"
            ],
            1,
        )
        self.store.mark_conversation_read(owner, self.project, cid)
        self.assertEqual(
            next(c for c in self.store.project_view(owner, self.project)["conversations"] if c["id"] == cid)[
                "unread_count"
            ],
            0,
        )


class SignedConversationApiTests(unittest.TestCase):
    def test_signed_project_api_does_not_disclose_other_humans_dm(self):
        class QuietBridge(AppBridge):
            def _enroll(self, server=None):
                return None

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(root / "service"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = "http://127.0.0.1:" + str(server.server_port)
            a, b, c = (QuietBridge(root / name) for name in ("a", "b", "c"))

            def call(bridge, op, payload):
                response = bridge.invoke(op, payload)
                self.assertTrue(response["ok"], response)
                return response["result"]

            try:
                for bridge, name in ((a, "alice"), (b, "bob"), (c, "charlie")):
                    call(
                        bridge,
                        "register",
                        {"username": name, "password": "pw", "server": address},
                    )
                Store(root / "service").claim_local_host(a.identity["account_id"], a.profile["node"])
                project = call(a, "new-project", {"name": "private test"})["id"]
                for bridge in (b, c):
                    invite = call(a, "invite", {"project": project})["invite"]
                    call(bridge, "join-project", {"server": address, "invite": invite})
                sent = call(
                    a,
                    "message",
                    {
                        "project": project,
                        "body": "Alice and Bob only",
                        "recipient_id": b.identity["account_id"],
                    },
                )
                self.assertTrue(sent["conversation_id"].startswith("human_human:"))
                self.assertEqual(
                    [m["body"] for m in call(b, "project", {"id": project})["messages"]],
                    ["Alice and Bob only"],
                )
                charlie = call(c, "project", {"id": project})
                self.assertEqual(charlie["messages"], [])
                self.assertEqual([x["kind"] for x in charlie["conversations"]], ["group"])
                response = c.invoke(
                    "message",
                    {
                        "project": project,
                        "body": "forge",
                        "conversation_id": sent["conversation_id"],
                    },
                )
                self.assertFalse(response["ok"])
                self.assertEqual(call(c, "project", {"id": project})["messages"], [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
