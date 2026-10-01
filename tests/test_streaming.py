import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_bridge import AppBridge
from appserver_client import PublicReplyDecoder
from collaboration_store import Store
from coordinator import Coordinator
from panel import action as panel_action


class Backend:
    def __init__(self):
        self.result = {'status': 'running'}
        self.submissions = []

    def submit(self, project, context, stage, scope, acceptance, validation):
        self.submissions.append(json.loads(context))
        return {'job': 'job-' + str(len(self.submissions))}

    def status(self, job, fetch=True):
        return self.result


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.owner = self.store.signup('owner', 'password', bootstrap=True)['id']
        self.project = self.store.project_create(self.owner, 'project')['id']
        self.backend = Backend()
        with self.store.connect() as db:
            db.execute("INSERT INTO nodes(name,owner_id) VALUES('node',?)", (self.owner,))
            for agent, name in (('admin', 'Admin'), ('peer', 'Peer')):
                db.execute("INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout,enabled) VALUES(?,?,?,?,?,?,?,?,?,1)",
                           (agent, self.project, name, self.owner, 'node', agent, 'model', 'low', 30))
        self.store.set_admin(self.owner, self.project, 'admin')
        self.coordinator = Coordinator(self.store, self.backend)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reply_stream_stays_above_midturn_human_message(self):
        self.store.message(self.owner, self.project, 'first')
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.store.message(self.owner, self.project, 'second')
        self.backend.result = {'status': 'running', 'reply_draft': 'partial'}
        self.coordinator.collect()
        view = self.store.project_view(self.owner, self.project)
        self.assertEqual([m['body'] for m in view['messages']], ['first', 'partial', 'second'])
        draft_id = view['messages'][1]['id']
        self.assertEqual([m['body'] for m in self.store.visible_messages('peer')], ['first', 'second'])
        self.backend.result = {'status': 'needs_review', 'report': json.dumps({'reply': 'finished', 'done': True,
                                'messages': [], 'tasks': [], 'executions': []})}
        self.coordinator.collect()
        view = self.store.project_view(self.owner, self.project)
        self.assertEqual([m['body'] for m in view['messages']], ['first', 'finished', 'second'])
        self.assertEqual(view['messages'][1]['id'], draft_id)

    def test_agent_can_finish_without_a_reply(self):
        self.store.message(self.owner, self.project, 'notice')
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.backend.result = {'status': 'needs_review', 'report': json.dumps({'reply': '', 'done': True,
                                'messages': [], 'tasks': [], 'executions': []})}
        self.coordinator.collect()
        self.assertEqual([m['body'] for m in self.store.project_view(self.owner, self.project)['messages']], ['notice'])
        self.coordinator.wake_messages()
        self.assertEqual(len(self.backend.submissions), 1)

    def test_private_stream_remains_addressed_to_sender(self):
        self.store.message(self.owner, self.project, 'private question', recipient='peer')
        self.coordinator.wake_messages()
        self.coordinator.dispatch()
        self.backend.result = {'status': 'running', 'reply_draft': 'private partial'}
        self.coordinator.collect()
        view = self.store.project_view(self.owner, self.project)
        partial = next(m for m in view['messages'] if m['body'] == 'private partial')
        self.assertEqual(partial['recipient_id'], self.owner)
        self.assertNotIn('private partial', [m['body'] for m in self.store.visible_messages('admin')])

    def test_public_mention_routes_to_only_named_agent(self):
        self.store.message(self.owner, self.project, 'Please review', mention_agent_id='peer')
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            tasks = [dict(r) for r in db.execute('SELECT agent_id FROM tasks')]
        self.assertEqual([t['agent_id'] for t in tasks], ['peer'])
        message = self.store.project_view(self.owner, self.project)['messages'][0]
        self.assertEqual(message['mention_agent_name'], 'Peer')
        with self.assertRaises(ValueError):
            self.store.message(self.owner, self.project, 'bad', mention_agent_id='missing')
        with self.assertRaises(ValueError):
            self.store.message(self.owner, self.project, 'bad', recipient='admin', mention_agent_id='peer')

    def test_zero_legacy_budget_does_not_block_mentions_or_group_chat(self):
        with self.store.connect() as db:
            db.execute('UPDATE projects SET budget_remaining=0 WHERE id=?', (self.project,))
        self.store.message(self.owner, self.project, 'general update')
        self.store.message(self.owner, self.project, 'please answer', mention_agent_id='peer')
        self.coordinator.wake_messages()
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            self.assertEqual([r['agent_id'] for r in db.execute('SELECT agent_id FROM tasks')], ['peer','admin'])
            self.assertEqual(db.execute('SELECT budget_remaining FROM projects WHERE id=?',
                                        (self.project,)).fetchone()[0], 0)

    def test_plain_at_text_is_not_a_mention(self):
        self.store.message(self.owner, self.project, '@Peer please review')
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            tasks = [r['agent_id'] for r in db.execute('SELECT agent_id FROM tasks')]
        self.assertEqual(tasks, ['admin'])

    def test_legacy_panel_action_preserves_explicit_mention(self):
        panel_action(self.store, {'id': self.owner}, {'action': 'message', 'project': self.project,
                     'body': 'Please review', 'mention_agent_id': 'peer'}, self.backend)
        self.coordinator.wake_messages()
        with self.store.connect() as db:
            self.assertEqual([r['agent_id'] for r in db.execute('SELECT agent_id FROM tasks')], ['peer'])

    def test_desktop_refuses_message_when_team_service_is_too_old(self):
        bridge = AppBridge(Path(self.tmp.name) / 'app')
        bridge.identity = {'public_key': 'unused'}
        bridge.profile = {'server': 'http://127.0.0.1:8765'}
        with patch.object(bridge, '_post', return_value={'nonce': 'nonce'}) as post:
            with self.assertRaisesRegex(RuntimeError, '版本过旧'):
                bridge._rpc('message', {'project': self.project, 'body': 'hello'})
        self.assertEqual(post.call_count, 1)

    def test_public_reply_decoder_never_emits_private_fields(self):
        decoder = PublicReplyDecoder()
        pieces = ['{"reply":"hello', ' world","messages":[{"body":"SECRET"}],', '"tasks":[]}']
        self.assertEqual([decoder.feed(part) for part in pieces], ['hello', 'hello world', 'hello world'])
        self.assertNotIn('SECRET', decoder.feed(''))
        self.assertEqual(PublicReplyDecoder().feed('{"messages":[{"body":"SECRET"}],"reply":"late"}'), '')


if __name__ == '__main__':
    unittest.main()
