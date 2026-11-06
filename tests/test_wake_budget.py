"""A chain of agent messages must not spend the team's budget unattended."""

import unittest

from agent_harness.coordinator import AGENT_WAKE_LIMIT, Coordinator
from tests import test_collaboration as fixtures


class WakeBudgetTests(unittest.TestCase):
    setUp = fixtures.CollaborationTests.setUp
    tearDown = fixtures.CollaborationTests.tearDown
    add_agent = fixtures.CollaborationTests.add_agent

    def agents(self):
        first = self.add_agent(self.owner["id"], "backend-a", "First")
        second = self.add_agent(self.owner["id"], "backend-b", "Second")
        with self.store.connect() as db:
            # Agents enter "configuring" until their machine confirms; the wake
            # scheduler only considers confirmed ones, and group posts wake the
            # project's administrator.
            db.execute("UPDATE agents SET configuring=0 WHERE id IN (?,?)", (first, second))
            db.execute("UPDATE projects SET admin_agent_id=? WHERE id=?", (first, self.project))
        return first, second

    def tasks(self):
        with self.store.connect() as db:
            return db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    def finish_tasks(self):
        # A real turn ends by consuming its message and completing the task; the
        # simulation has to do both, or the same message keeps being "new".
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET state='completed'")
            latest = db.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0
            for row in db.execute("SELECT DISTINCT agent_id FROM tasks").fetchall():
                db.execute(
                    "INSERT INTO agent_cursors(agent_id,channel,last_message) VALUES(?,?,?) "
                    "ON CONFLICT(agent_id,channel) DO UPDATE SET last_message=excluded.last_message",
                    (row["agent_id"], "group", latest),
                )

    def post(self, sender, kind, body):
        with self.store.connect() as db:
            self.store._message(db, self.project, sender, kind, body)

    def test_a_long_agent_chain_stops_and_records_why(self):
        first, second = self.agents()
        coordinator = Coordinator(self.store, self.backend)
        for index in range(AGENT_WAKE_LIMIT + 4):
            self.post(second, "agent", "agent note %d" % index)
            coordinator.wake_messages()
            self.finish_tasks()
        self.assertLessEqual(self.tasks(), AGENT_WAKE_LIMIT)
        with self.store.connect() as db:
            logged = db.execute("SELECT COUNT(*) FROM audit WHERE action='agent_wake_budget'").fetchone()[0]
        self.assertGreater(logged, 0)

    def test_a_human_message_restarts_the_budget(self):
        first, second = self.agents()
        coordinator = Coordinator(self.store, self.backend)
        for index in range(AGENT_WAKE_LIMIT + 4):
            self.post(second, "agent", "agent note %d" % index)
            coordinator.wake_messages()
            self.finish_tasks()
        before = self.tasks()
        self.post(self.owner["id"], "human", "human takes over")
        coordinator.wake_messages()
        self.assertGreater(self.tasks(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
