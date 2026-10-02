import json
import tempfile
import unittest

from collaboration_store import Store
from coordinator import Coordinator
from project_memory import ProjectMemory


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.owner = self.store.signup('owner', 'password', bootstrap=True)['id']
        self.project = self.store.project_create(self.owner, 'Research')['id']
        self.memory = ProjectMemory(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_project_notes_are_shared_and_owner_can_delete(self):
        invite = self.store.invite(self.owner, self.project)['invite']
        peer = self.store.signup('peer', 'password', invite=invite)['id']
        note = self.memory.add_note(peer, self.project, 'Public project decision')
        self.assertEqual(self.memory.snapshot(self.owner, self.project)['notes'][0]['body'],
                         'Public project decision')
        self.assertEqual(self.memory.agent_context(self.project)[0]['author_id'], peer)
        self.assertEqual(self.memory.delete_note(self.owner, self.project, note['id']),
                         {'deleted': True})
        self.assertEqual(self.memory.snapshot(peer, self.project)['notes'], [])

    def test_nonmember_cannot_read_or_write_memory(self):
        outsider = self.store.signup('outsider', 'password', bootstrap=False,
                                     invite=self.store.invite(self.owner, self.project)['invite'])['id']
        with self.store.connect() as db:
            db.execute('DELETE FROM project_users WHERE project_id=? AND user_id=?',
                       (self.project, outsider))
        with self.assertRaises(PermissionError):
            self.memory.snapshot(outsider, self.project)
        with self.assertRaises(PermissionError):
            self.memory.add_note(outsider, self.project, 'secret')

    def test_private_turn_does_not_add_shared_note(self):
        # Even a malicious structured reply cannot promote a private statement.
        with self.store.connect() as db:
            db.execute('INSERT INTO nodes(name,owner_id) VALUES(?,?)', ('local', self.owner))
            db.execute('''INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,
                model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)''',
                ('agent', self.project, 'Agent', self.owner, 'local', 'local-project',
                 'model', 'medium', 60))
        self.store.set_admin(self.owner, self.project, 'agent')
        task = self.store.task(self.owner, {'project': self.project, 'agent': 'agent',
            'title': 'Private', 'instructions': 'Reply', 'acceptance': 'Reply',
            'validation': 'Review', 'private_turn': True, 'reply_to': self.owner})['id']
        report = json.dumps({'reply': 'private answer', 'done': True, 'messages': [],
            'tasks': [], 'executions': [], 'memory_notes': ['private information']})
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET state=?,report=? WHERE id=?',
                       ('needs_review', report, task))
        Coordinator(self.store).process_completed(task)
        self.assertEqual(self.memory.snapshot(self.owner, self.project)['notes'], [])

    def test_shared_note_survives_switching_between_group_and_private_contexts(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO nodes(name,owner_id) VALUES(?,?)', ('local', self.owner))
            db.execute('''INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,
                model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)''',
                ('agent', self.project, 'Agent', self.owner, 'local', 'local-project',
                 'model', 'medium', 60))
            agent = dict(db.execute('SELECT * FROM agents WHERE id=?', ('agent',)).fetchone())
        self.memory.add_note(self.owner, self.project, 'Shared decision across chats')
        coordinator = Coordinator(self.store)
        for contract in ({'private_turn': False},
                         {'private_turn': True, 'reply_to': self.owner}):
            context = coordinator.agent_context(agent, {'contract': json.dumps(contract), 'cutoff': 0})
            self.assertIn('Shared decision across chats', context)


if __name__ == '__main__':
    unittest.main()
