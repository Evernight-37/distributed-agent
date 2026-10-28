"""Privacy-preserving per-account event cursors."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer

from agent_harness.app_bridge import AppBridge
from agent_harness.broker import PullBackend
from agent_harness.collaboration_store import Store
from tests.test_collaboration import FakeBackend
from agent_harness.panel import handler_for
from tests.helpers import provision_account


class EventCursorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.owner = provision_account(self.store, "owner", bootstrap=True)["id"]
        self.project = self.store.project_create(self.owner, "events")["id"]
        invite = self.store.invite(self.owner, self.project)["invite"]
        self.peer = provision_account(self.store, "peer", invite=invite)["id"]
        invite = self.store.invite(self.owner, self.project)["invite"]
        self.third = provision_account(self.store, "third", invite=invite)["id"]
        PullBackend(self.store).create_pair(self.owner, "lab")
        self.backend = FakeBackend()
        self.backend.projects.update(
            {
                "one": {"node": "lab"},
                "two": {"node": "lab"},
            }
        )
        self.first = self._agent("one", "first")
        self.second = self._agent("two", "second")

    def tearDown(self):
        self.temporary.cleanup()

    def _agent(self, project, name):
        return self.store.add_agent(
            self.owner,
            {
                "project": self.project,
                "name": name,
                "node": "lab",
                "backend_project": project,
                "model": "gpt-test",
                "effort": "low",
                "timeout": 30,
            },
            self.backend,
        )["id"]

    def cursors(self):
        return {user: self.store.event_cursor(user) for user in (self.owner, self.peer, self.third)}

    def test_group_and_agent_internal_events_wake_all_members(self):
        before = self.cursors()
        self.store.message(self.owner, self.project, "group")
        after = self.cursors()
        self.assertTrue(all(after[user] == before[user] + 1 for user in before))

        before = after
        with self.store.connect() as db:
            self.store._message(db, self.project, self.first, "agent", "internal", self.second)
        after = self.cursors()
        self.assertTrue(all(after[user] == before[user] + 1 for user in before))

    def test_human_agent_private_event_wakes_only_human_participant(self):
        before = self.cursors()
        self.store.message(self.owner, self.project, "private", self.first)
        after = self.cursors()
        self.assertEqual(after[self.owner], before[self.owner] + 1)
        self.assertEqual(after[self.peer], before[self.peer])
        self.assertEqual(after[self.third], before[self.third])

    def test_human_human_private_event_wakes_only_both_participants(self):
        before = self.cursors()
        self.store.message(self.owner, self.project, "private", self.peer)
        after = self.cursors()
        self.assertEqual(after[self.owner], before[self.owner] + 1)
        self.assertEqual(after[self.peer], before[self.peer] + 1)
        self.assertEqual(after[self.third], before[self.third])

    def test_wait_returns_change_or_bounded_heartbeat(self):
        cursor = self.store.event_cursor(self.owner)
        started = time.monotonic()
        unchanged = self.store.events_wait(self.owner, cursor, 0.01)
        self.assertFalse(unchanged["changed"])
        self.assertEqual(unchanged["cursor"], cursor)
        self.assertLess(time.monotonic() - started, 0.5)

        self.store.message(self.owner, self.project, "new")
        changed = self.store.events_wait(self.owner, cursor, 0)
        self.assertTrue(changed["changed"])
        self.assertGreater(changed["cursor"], cursor)

    def test_wait_rejects_invalid_identity_cursor_and_timeout(self):
        with self.assertRaises(PermissionError):
            self.store.events_wait("missing", 0, 0)
        for cursor in (-1, True, "1"):
            with self.assertRaises(ValueError):
                self.store.events_wait(self.owner, cursor, 0)
        for timeout in (-1, True, 21):
            with self.assertRaises(ValueError):
                self.store.events_wait(self.owner, 0, timeout)

    def test_event_rows_contain_no_message_or_identity_details(self):
        secret = "SHOULD-NOT-BE-STORED-IN-EVENTS"
        self.store.message(self.owner, self.project, secret, self.first)
        with self.store.connect() as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(account_events)")]
            serialized = "\n".join(str(tuple(row)) for row in db.execute("SELECT * FROM account_events"))
        self.assertEqual(columns, ["account_id", "cursor", "project_id", "kind", "created_at"])
        self.assertNotIn(secret, serialized)
        self.assertNotIn(self.first, serialized)


class SignedEventWaitTests(unittest.TestCase):
    def test_signed_identity_can_wait_for_its_private_cursor(self):
        class QuietBridge(AppBridge):
            def _enroll(self, server=None):
                return None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(root / "hub"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = "http://127.0.0.1:" + str(server.server_port)
            bridge = QuietBridge(root / "desktop")
            try:
                result = bridge.invoke(
                    "register",
                    {"username": "owner", "password": "", "server": address},
                )
                self.assertTrue(result["ok"], result)
                Store(root / "hub").claim_local_host(bridge.identity["account_id"], bridge.profile["node"])
                created = bridge.invoke("new-project", {"name": "events"})
                self.assertTrue(created["ok"], created)
                event = bridge._event_wait(bridge.identity, address, 0, 0)
                self.assertTrue(event["changed"])
                self.assertGreater(event["cursor"], 0)
            finally:
                bridge.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
