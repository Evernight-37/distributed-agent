"""Agent messages continue scheduling while there is new work to process."""

import json
import unittest

from agent_harness.coordinator import Coordinator
from tests import test_collaboration as fixtures


class AgentWakeTests(unittest.TestCase):
    setUp = fixtures.CollaborationTests.setUp
    tearDown = fixtures.CollaborationTests.tearDown
    add_agent = fixtures.CollaborationTests.add_agent

    def agents(self):
        first = self.add_agent(self.owner["id"], "backend-a", "First")
        second = self.add_agent(self.owner["id"], "backend-b", "Second")
        with self.store.connect() as db:
            db.execute("UPDATE agents SET configuring=0 WHERE id IN (?,?)", (first, second))
            db.execute("UPDATE projects SET admin_agent_id=? WHERE id=?", (first, self.project))
        return first, second

    def post(self, sender, body, recipient=None):
        with self.store.connect() as db:
            self.store._message(db, self.project, sender, "agent", body, recipient)

    def test_long_group_discussion_does_not_wake_the_administrator(self):
        _, peer = self.agents()
        coordinator = Coordinator(self.store, self.backend)
        for index in range(16):
            self.post(peer, f"Agent note {index}")
            coordinator.wake_messages()
            coordinator.dispatch()
            coordinator.collect()

        self.assertEqual(self.backend.submissions, [])

    def test_private_exchange_continues_past_twenty_four_turns_and_stops_on_empty_reply(self):
        first, second = self.agents()
        coordinator = Coordinator(self.store, self.backend)
        self.post(first, "Kickoff", second)

        for index in range(28):
            # Scheduling twice while the same message is pending must not enqueue a duplicate.
            coordinator.wake_messages()
            coordinator.wake_messages()
            coordinator.dispatch()
            self.assertEqual(len(self.backend.submissions), index + 1)
            _, backend, context, _ = self.backend.submissions[-1]
            self.assertEqual(backend, "backend-a" if index % 2 else "backend-b")
            task = json.loads(context)["task"]
            self.assertTrue(task["private_turn"])
            self.assertEqual(task["reply_to"], second if index % 2 else first)
            self.backend.report = json.dumps({"reply": f"Reply {index}" if index < 27 else ""})
            coordinator.collect()

        # The new coordinator has the same durable message cursors after restart.
        Coordinator(self.store, self.backend).tick()
        self.assertEqual(len(self.backend.submissions), 28)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 28)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM tasks WHERE state='needs_review' AND processed=1").fetchone()[0], 28
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
