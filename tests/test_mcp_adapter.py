import tempfile
import threading
import unittest
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from account_identity import approve_delegate, generate_identity
from app_bridge import AppBridge, MCP_DELEGATE_SCOPES
from collaboration_store import Store
from common import write_json
from mcp_adapter import HarnessMcpBackend
from panel import handler_for


class QuietBridge(AppBridge):
    def _enroll(self,server=None):return None


class McpAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.state=self.root/'service';self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.state))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.bridge=QuietBridge(self.root/'app')
        result=self.bridge.invoke('register',{'username':'Owner','password':'local','server':self.url})
        self.assertTrue(result['ok'],result)
        Store(self.state).claim_local_host(self.bridge.identity['account_id'],self.bridge.profile['node'])
        self.project=self.bridge._rpc('create-project',{'name':'MCP project'},server=self.url)['id']
        delegate=generate_identity();delegate.update(account_id=self.bridge.identity['account_id'],
            chain=approve_delegate(self.bridge.identity,delegate['public_key'],MCP_DELEGATE_SCOPES))
        self.grant=self.root/'grant.json'
        write_json(self.grant,{'version':1,'profile_id':self.bridge.profile['id'],'username':'Owner',
            'identity':delegate,'scopes':list(MCP_DELEGATE_SCOPES),'servers':[self.url]})
        self.bridge._register_mcp_delegate(self.url,{'identity':delegate})
        self.backend=HarnessMcpBackend(self.grant)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2);self.temp.cleanup()

    def test_lists_reads_and_sends_without_browser_session(self):
        status=self.backend.call('harness_status',{})
        self.assertEqual(status['account'],'Owner');self.assertEqual(status['unavailable_services'],[])
        projects=self.backend.call('harness_projects_list',{})['projects']
        self.assertEqual(projects[0]['id'],self.project)
        sent=self.backend.call('harness_message_send',{'project':self.project,'body':'Hello from normal Codex'})
        view=self.backend.call('harness_project_read',{'project':self.project,'after_message_id':0})
        self.assertEqual(view['messages'][-1]['id'],sent['id'])
        self.assertEqual(view['messages'][-1]['body'],'Hello from normal Codex')
        self.assertEqual(view['messages'][-1]['sender_client'],'codex')

    def test_assigns_owned_agent_asynchronously_and_reads_status(self):
        account=self.bridge.identity['account_id'];node=self.bridge.profile['node']
        with Store(self.state).connect() as db:
            db.execute('INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)',
                ('agent-owned',self.project,'Owned worker',account,node,'backend','model','medium',120))
        task=self.backend.call('harness_task_assign',{'project':self.project,'agent':'agent-owned','title':'Review',
            'instructions':'Review the change','stage':'review','acceptance':'Report concrete findings','validation':'Inspect the diff'})
        self.assertEqual(task['state'],'queued')
        state=self.backend.call('harness_task_get',{'project':self.project,'task':task['id']})
        self.assertEqual(state['state'],'queued')
        self.assertEqual(__import__('json').loads(state['contract'])['requested_via'],'codex')

    def test_unknown_project_and_invalid_message_id_fail_closed(self):
        with self.assertRaises(ValueError):self.backend.call('harness_project_read',{'project':'missing'})
        with self.assertRaises(ValueError):self.backend.call('harness_message_send',{
            'project':self.project,'body':'x','client_message_id':'not-a-uuid'})

    def test_desktop_enable_and_disable_use_named_codex_entry(self):
        configured={};calls=[]
        def codex(*arguments,check=True):
            calls.append(arguments)
            action,name=arguments[:2]
            if action=='get':
                value=configured.get(name)
                return SimpleNamespace(returncode=0 if value else 1,stdout=__import__('json').dumps(value) if value else '',stderr='')
            if action=='add':
                launch=list(arguments[3:]);configured[name]={'name':name,'transport':{'type':'stdio','command':launch[0],'args':launch[1:]}}
            if action=='remove':configured.pop(name,None)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(self.bridge,'_codex_mcp',side_effect=codex):
            enabled=self.bridge._enable_mcp()
            self.assertTrue(enabled['enabled']);self.assertTrue(enabled['configured'])
            self.assertNotIn('grant',enabled);self.assertNotIn('configuration',enabled)
            self.assertTrue(self.bridge._mcp_grant_path().is_file())
            mcp_name=self.bridge._mcp_name();self.assertTrue(mcp_name.startswith('agent-harness-'))
            add=next(call for call in calls if call[:2]==('add',mcp_name))
            self.assertIn('mcp_adapter.py',' '.join(map(str,add)))
            disabled=self.bridge._disable_mcp()
            self.assertFalse(disabled['enabled']);self.assertFalse(self.bridge._mcp_grant_path().exists())
        with Store(self.state).connect() as db:
            delegate=db.execute('SELECT revoked FROM account_delegates ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(delegate['revoked'],1)

    def test_offline_revocation_is_retained_and_retried(self):
        configured={};offline={'value':False}
        original_rpc=self.bridge._rpc
        def rpc(operation,payload=None,server=None,timeout=15):
            if operation=='revoke-delegate' and offline['value']:raise RuntimeError('offline')
            return original_rpc(operation,payload,server,timeout)
        def codex(*arguments,check=True):
            action,name=arguments[:2]
            if action=='get':
                value=configured.get(name);return SimpleNamespace(returncode=0 if value else 1,stdout=__import__('json').dumps(value) if value else '',stderr='')
            if action=='add':
                launch=list(arguments[3:]);configured[name]={'name':name,'transport':{'type':'stdio','command':launch[0],'args':launch[1:]}}
            if action=='remove':configured.pop(name,None)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(self.bridge,'_codex_mcp',side_effect=codex),patch.object(self.bridge,'_rpc',side_effect=rpc):
            self.bridge._enable_mcp();offline['value']=True
            result=self.bridge._disable_mcp()
            self.assertEqual(result['pending_revocations'],1)
            self.assertTrue(self.bridge._mcp_revocation_path().is_file())
            self.assertFalse(self.bridge._mcp_grant_path().exists())
            offline['value']=False
            self.assertEqual(self.bridge._retry_mcp_revocations(),[])
            self.assertFalse(self.bridge._mcp_revocation_path().exists())

    def test_two_offline_delegate_generations_are_both_retained(self):
        configured={};offline={'value':False};revoked=[]
        original_rpc=self.bridge._rpc
        def rpc(operation,payload=None,server=None,timeout=15):
            if operation=='revoke-delegate':
                if offline['value']:raise RuntimeError('offline')
                revoked.append(payload['public_key'])
            return original_rpc(operation,payload,server,timeout)
        def codex(*arguments,check=True):
            action,name=arguments[:2]
            if action=='get':
                value=configured.get(name);return SimpleNamespace(returncode=0 if value else 1,stdout=__import__('json').dumps(value) if value else '',stderr='')
            if action=='add':
                launch=list(arguments[3:]);configured[name]={'name':name,'transport':{'type':'stdio','command':launch[0],'args':launch[1:]}}
            if action=='remove':configured.pop(name,None)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(self.bridge,'_codex_mcp',side_effect=codex),patch.object(self.bridge,'_rpc',side_effect=rpc):
            first=self.bridge._enable_mcp();self.assertTrue(first['configured']);offline['value']=True
            self.bridge._disable_mcp()
            first_public=__import__('json').loads(self.bridge._mcp_revocation_path().read_text())['entries'][0]['public_key']
            self.bridge._enable_mcp();second_public=__import__('json').loads(self.bridge._mcp_grant_path().read_text())['identity']['public_key']
            self.assertNotEqual(first_public,second_public)
            self.bridge._disable_mcp()
            entries=__import__('json').loads(self.bridge._mcp_revocation_path().read_text())['entries']
            self.assertEqual({entry['public_key'] for entry in entries},{first_public,second_public})
            offline['value']=False;self.assertEqual(self.bridge._retry_mcp_revocations(),[])
            self.assertEqual(set(revoked),{first_public,second_public})
            self.assertFalse(self.bridge._mcp_revocation_path().exists())

    def test_profile_specific_name_does_not_replace_foreign_entry(self):
        foreign={'name':self.bridge._mcp_name(),'transport':{'type':'stdio','command':'other.exe','args':[]}}
        def codex(*arguments,check=True):
            if arguments[0]=='get':return SimpleNamespace(returncode=0,stdout=__import__('json').dumps(foreign),stderr='')
            self.fail('foreign entry must not be modified')
        with patch.object(self.bridge,'_codex_mcp',side_effect=codex):
            with self.assertRaisesRegex(RuntimeError,'同名'):self.bridge._enable_mcp()


if __name__=='__main__':unittest.main()
