"""Server-side conversation authorization and legacy migration."""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from app_bridge import AppBridge
from collaboration_store import Store
from panel import action, handler_for
from tests import test_collaboration as fixtures


class ConversationPrivacyTests(unittest.TestCase):
    setUp = fixtures.CollaborationTests.setUp
    tearDown = fixtures.CollaborationTests.tearDown
    add_agent = fixtures.CollaborationTests.add_agent

    def people_and_agents(self):
        third = self.store.signup('third', 'password', invite=self.store.invite(self.owner['id'], self.project)['invite'])
        first = self.add_agent(self.owner['id'], 'backend-a', 'First')
        second = self.add_agent(self.owner['id'], 'backend-b', 'Second')
        return third['id'], first, second

    def bodies(self, user):
        return [m['body'] for m in self.store.project_view(user, self.project)['messages']]

    def test_four_conversation_classes_and_agent_visibility(self):
        third, first, second = self.people_and_agents()
        owner, other = self.owner['id'], self.other['id']
        self.store.message(owner, self.project, 'public')
        ha = self.store.message(owner, self.project, 'human agent secret', first)
        hh = self.store.message(owner, self.project, 'human human secret', other)
        with self.store.connect() as db:
            aa = self.store._message(db, self.project, first, 'agent', 'internal agent note', second)
        self.assertEqual(self.bodies(owner), ['public', 'human agent secret', 'human human secret', 'internal agent note'])
        self.assertEqual(self.bodies(other), ['public', 'human human secret', 'internal agent note'])
        self.assertEqual(self.bodies(third), ['public', 'internal agent note'])
        self.assertEqual([m['body'] for m in self.store.visible_messages(second)], ['public', 'internal agent note'])
        self.assertNotIn('human human secret', [m['body'] for m in self.store.visible_messages(first)])
        own = self.store.project_view(owner, self.project)
        self.assertEqual(ha['conversation_id'], 'human_agent:' + owner + ':' + first)
        self.assertEqual(hh['conversation_id'], 'human_human:' + ':'.join(sorted((owner, other))))
        self.assertEqual(aa['conversation_id'], 'agent_agent:' + ':'.join(sorted((first, second))))
        self.assertEqual({c['kind'] for c in own['conversations']}, {'group', 'human_agent', 'human_human', 'agent_agent'})
        self.assertEqual({c['kind'] for c in self.store.project_view(third, self.project)['conversations']}, {'group', 'agent_agent'})

    def test_conversation_id_cannot_override_recipient_or_impersonate_agent(self):
        _, first, second = self.people_and_agents()
        owner, other = self.owner['id'], self.other['id']
        private = self.store.message(owner, self.project, 'start', first)['conversation_id']
        self.store.message(owner, self.project, 'followup', conversation_id=private)
        with self.assertRaises(PermissionError):
            self.store.message(other, self.project, 'intrusion', conversation_id=private)
        with self.assertRaises(ValueError):
            self.store.message(owner, self.project, 'mismatch', recipient=second, conversation_id=private)
        with self.assertRaises(PermissionError):
            self.store.message(owner, self.project, 'impersonation', conversation_id='agent_agent:' + ':'.join(sorted((first, second))))
        with self.assertRaises(ValueError):
            self.store.message(owner, self.project, 'self', recipient=owner)

    def test_legacy_backfill_never_exposes_direct_message_to_other_human(self):
        third, first, second = self.people_and_agents()
        owner, other = self.owner['id'], self.other['id']
        with self.store.connect() as db:
            rows = [(owner, 'human', first, 'legacy human agent'),
                    (first, 'agent', owner, 'legacy answer'),
                    (owner, 'human', other, 'legacy human human'),
                    (first, 'agent', second, 'legacy agent agent'),
                    (owner, 'human', 'unknown', 'orphan private')]
            for sender,kind,recipient,body in rows:
                db.execute('INSERT INTO messages(project_id,sender_id,sender_type,recipient_id,body,created_at) VALUES(?,?,?,?,?,1)',
                           (self.project,sender,kind,recipient,body))
        self.store = Store(self.root)  # reopening performs the migration
        self.assertEqual(self.bodies(third), ['legacy agent agent'])
        self.assertEqual(self.bodies(other), ['legacy human human', 'legacy agent agent'])
        self.assertEqual(self.bodies(owner), ['legacy human agent', 'legacy answer', 'legacy human human', 'legacy agent agent'])

    def test_private_task_contract_and_report_are_not_in_other_humans_view(self):
        _, first, _ = self.people_and_agents()
        owner, other = self.owner['id'], self.other['id']
        task = self.store.task(owner, {'project':self.project,'agent':first,'title':'private title',
            'instructions':'PRIVATE CONTRACT','stage':'discuss','allowed_files':'read only',
            'acceptance':'PRIVATE ACCEPTANCE','validation':'check',
            'private_turn':True,'reply_to':owner})['id']
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET report=? WHERE id=?', (json.dumps({'reply':'PRIVATE REPORT'}),task))
        other_view = self.store.project_view(other,self.project)
        self.assertEqual(other_view['tasks'],[])
        self.assertEqual(other_view['messages'],[])
        self.assertEqual(len(self.store.project_view(owner,self.project)['tasks']),1)
        with self.assertRaises(PermissionError):
            action(self.store,{'id':other},{'action':'refresh-task','task':task},self.backend)

    def test_read_cursor_is_per_conversation_and_authorized(self):
        _, first, _ = self.people_and_agents()
        owner, other = self.owner['id'], self.other['id']
        cid = self.store.message(owner,self.project,'private',first)['conversation_id']
        with self.assertRaises(PermissionError):
            self.store.mark_conversation_read(other,self.project,cid)
        own = self.store.project_view(owner,self.project)
        self.assertEqual(next(c for c in own['conversations'] if c['id']==cid)['unread_count'],0)
        with self.store.connect() as db:
            self.store._message(db,self.project,first,'agent','response',owner)
        self.assertEqual(next(c for c in self.store.project_view(owner,self.project)['conversations'] if c['id']==cid)['unread_count'],1)
        self.store.mark_conversation_read(owner,self.project,cid)
        self.assertEqual(next(c for c in self.store.project_view(owner,self.project)['conversations'] if c['id']==cid)['unread_count'],0)


class SignedConversationApiTests(unittest.TestCase):
    def test_signed_project_api_does_not_disclose_other_humans_dm(self):
        class QuietBridge(AppBridge):
            def _enroll(self, server=None):
                return None

        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(root/'service'))
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            address='http://127.0.0.1:'+str(server.server_port)
            a,b,c=(QuietBridge(root/name) for name in ('a','b','c'))

            def call(bridge,op,payload):
                response=bridge.invoke(op,payload)
                self.assertTrue(response['ok'],response)
                return response['result']

            try:
                for bridge,name in ((a,'alice'),(b,'bob'),(c,'charlie')):
                    call(bridge,'register',{'username':name,'password':'pw','server':address})
                project=call(a,'new-project',{'name':'private test'})['id']
                for bridge in (b,c):
                    invite=call(a,'invite',{'project':project})['invite']
                    call(bridge,'join-project',{'server':address,'invite':invite})
                sent=call(a,'message',{'project':project,'body':'Alice and Bob only','recipient_id':b.identity['account_id']})
                self.assertTrue(sent['conversation_id'].startswith('human_human:'))
                self.assertEqual([m['body'] for m in call(b,'project',{'id':project})['messages']],['Alice and Bob only'])
                charlie=call(c,'project',{'id':project})
                self.assertEqual(charlie['messages'],[])
                self.assertEqual([x['kind'] for x in charlie['conversations']],['group'])
                response=c.invoke('message',{'project':project,'body':'forge','conversation_id':sent['conversation_id']})
                self.assertFalse(response['ok'])
                self.assertEqual(call(c,'project',{'id':project})['messages'],[])
            finally:
                server.shutdown();server.server_close();thread.join()


if __name__ == '__main__':
    unittest.main()
