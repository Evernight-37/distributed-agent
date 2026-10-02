import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import os
from types import SimpleNamespace
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import read_json, write_json
from local_panel import handler_for
from pull_worker import Connector, deploy_runtime, validate_server, api, enroll


class PullWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.config=self.root/'config.json'
        write_json(self.config,{'root':str(self.root),'server':'http://localhost','device_token':'private-device-token-value'})
        write_json(self.root/'projects'/'local'/'project.json',{'name':'local','model':'owner-model','effort':'high','timeout':120,'remote_path':str(self.root)})
        self.calls=[];self.reports=[];self.jobs=[];self.cancellations=[];self.fail_report=False;self.local_state='needs_review'
        def executor(request):
            self.calls.append(request)
            if request['action']=='fetch':return {'status':self.local_state,'report':'local-report'}
            if request['action']=='execution_status':return {'status':'completed','exit_code':0}
            return {'status':'running'}
        def transport(config,operation,payload):
            if operation=='poll':return {'jobs':self.jobs,'projects':[],'cancellations':self.cancellations}
            if self.fail_report:raise OSError('offline')
            self.reports.append(payload);return {}
        self.connector=Connector(self.config,transport,executor)
        self.binding={'project_id':'p1','backend_project':'local','model':'owner-model','effort':'high','timeout':120,'stages':['discuss'],'enabled':True}
        self.connector.save_binding(self.binding)

    def job(self,name,kind='task',request=None):
        return {'id':name,'job_id':name,'lease_id':'fixed-lease','project_id':'p1','backend_project':'local','agent_id':'agent1','kind':kind,
            'request':request or {'prompt':json.dumps({'identity':{'id':'agent1'},'project':{'id':'p1'}}),'stage':'discuss','session_channel':'group'}}

    def starts(self,action='start'):
        return [r for r in self.calls if r['action']==action]

    def test_local_settings_and_duplicate_assignment(self):
        self.jobs=[self.job('turn')]
        for _ in range(4):self.connector.cycle()
        self.assertEqual(len(self.starts()),1)
        configured=self.starts('configure')[0]
        self.assertEqual((configured['model'],configured['effort']),('owner-model','high'))
        self.assertTrue(read_json(self.root/'projects'/'local'/'project.json')['local_control'])
        self.assertEqual(self.reports[-1]['state'],'needs_review')

    def test_remote_configuration_and_identity_are_rejected(self):
        for index,request in enumerate([{'model':'remote-model','argv':['evil']}, {'stage':'implement','prompt':'{}'},
            {'stage':'discuss','prompt':json.dumps({'identity':{'id':'wrong'},'project':{'id':'p1'}})}]):
            self.jobs=[self.job('bad'+str(index),request=request)];self.connector.cycle()
            self.assertEqual(self.reports[-1]['state'],'failed')
        self.assertFalse(self.starts())

    def test_optional_display_name_defaults_to_local_project(self):
        self.connector.save_binding(dict(self.binding,name=''))
        self.assertEqual(self.connector.advertisements()[0]['name'],'local')

    @unittest.skipUnless(os.name == 'nt', 'Windows credential permissions')
    def test_failed_credential_acl_removes_new_token_file(self):
        root = self.root/'enrollment'
        with patch('pull_worker.api',return_value={'node':'local'}), patch('pull_worker.subprocess.check_output',return_value='"user","S-1-5-21-123"'), patch('pull_worker.subprocess.run',return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(RuntimeError): enroll('https://example.com','one-time-code',root)
        self.assertFalse((root/'pull-config.json').exists())

    def test_execution_requires_local_approval_and_template_is_frozen(self):
        template=self.root/'projects'/'local'/'execution_templates.json'
        write_json(template,{'check':{'argv':['fixed-program','arg']}})
        self.jobs=[self.job('exec','execution',{'template':'check'})]
        self.connector.cycle();self.connector.cycle()
        self.assertFalse(self.starts('execution_start'))
        self.assertEqual(self.reports[-1]['result']['local_status'],'waiting_local_approval')
        self.connector.approve('exec',True);self.connector.cycle();self.connector.cycle()
        self.assertEqual(len(self.starts('execution_start')),1)
        self.jobs=[self.job('changed','execution',{'template':'check'})];self.connector.cycle()
        self.connector.approve('changed',True);write_json(template,{'check':{'argv':['other-program']}})
        self.connector.cycle();self.assertEqual(self.reports[-1]['state'],'failed')
        self.assertEqual(len(self.starts('execution_start')),1)

    def test_lost_report_retries_without_reexecution(self):
        self.jobs=[self.job('turn')];self.connector.cycle()
        self.fail_report=True
        with self.assertRaises(OSError):self.connector.cycle()
        self.fail_report=False;self.connector.cycle()
        self.assertEqual(len(self.starts()),1)
        self.assertEqual(self.reports[-1]['state'],'needs_review')

    def test_crash_launch_receipt_reconciles_instead_of_resubmitting(self):
        job=self.job('ambiguous');receipt=self.connector.accept(job)
        receipt['phase']='launching';write_json(self.connector.receipt_path('ambiguous'),receipt)
        self.jobs=[job];self.connector.cycle()
        self.assertFalse(self.starts());self.assertEqual(self.reports[-1]['state'],'needs_review')

    def test_disable_running_binding_does_not_cancel_existing_turn(self):
        self.local_state='running';self.jobs=[self.job('turn')];self.connector.cycle()
        self.connector.save_binding(dict(self.binding,enabled=False));self.connector.cycle()
        self.assertFalse(self.starts('cancel'))
        self.assertEqual(self.reports[-1]['state'],'running')
        self.local_state='cancelled';self.connector.cycle()
        self.assertEqual(self.reports[-1]['state'],'cancelled')

    def test_runtime_update_refuses_unknown_or_active_supervisor(self):
        state=self.root/'jobs'/'unknown'/'state.json';write_json(state,{'status':'unknown'})
        with self.assertRaises(ValueError):deploy_runtime(self.root)
        state.unlink();write_json(self.root/'projects'/'local'/'active.json',{'job':'missing-state'})
        with self.assertRaises(ValueError):deploy_runtime(self.root)

    def test_unknown_local_reconciliation_never_restarts(self):
        receipt=self.connector.accept(self.job('unknown'))
        receipt.update(phase='terminal',result={'status':'unknown'},reported_terminal=True)
        write_json(self.connector.receipt_path('unknown'),receipt)
        result=self.connector.resolve('unknown')
        self.assertEqual(result['status'],'needs_review');self.assertFalse(self.starts())
        self.connector.cycle();self.assertEqual(self.reports[-1]['state'],'needs_review')

    def test_transient_status_failure_never_requests_cancellation(self):
        receipt=self.connector.accept(self.job('transient'))
        receipt['phase']='running';write_json(self.connector.receipt_path('transient'),receipt)
        calls=[]
        def executor(request):
            calls.append(request['action'])
            if request['action']=='fetch':raise OSError('temporary observer failure')
            return {'status':'running'}
        self.connector.executor=executor
        result=self.connector.advance(receipt)
        self.assertEqual(result['phase'],'running')
        self.assertEqual(result['result']['status'],'unknown')
        self.assertEqual(calls,['fetch'])

    def test_unknown_no_job_requires_explicit_owner_confirmation(self):
        receipt=self.connector.accept(self.job('missing'))
        receipt.update(phase='terminal',result={'status':'unknown'},reported_terminal=True)
        write_json(self.connector.receipt_path('missing'),receipt)
        def missing(_):raise ValueError('Unknown job')
        self.connector.executor=missing
        with self.assertRaises(ValueError):self.connector.resolve('missing')
        self.assertEqual(self.connector.resolve('missing',True)['status'],'cancelled')
        self.assertFalse(self.starts())

    def test_non_loopback_broker_requires_https(self):
        for url in ('http://100.98.57.38:8765','http://example.com','https://user:pass@example.com','https://example.com/path'):
            with self.assertRaises(ValueError):validate_server(url)
        for url in ('https://example.com','http://127.0.0.1:18876','http://localhost:8765','http://[::1]:8765'):
            validate_server(url)

    def test_broker_redirect_is_rejected_without_following_it(self):
        calls=[]
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                calls.append(self.path);self.send_response(307)
                self.send_header('Location','/credential-receiver');self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        with self.assertRaises(urllib.error.HTTPError):api({'server':f'http://127.0.0.1:{server.server_port}','device_token':'test-token'},'poll',{})
        self.assertEqual(calls,['/api/device/poll'])

    def test_real_worker_registration_configuration(self):
        deploy_runtime(self.root);real=Connector(self.config)
        real.configure(self.binding)
        project=real.rpc({'action':'project','project':'local'})['project']
        self.assertEqual(project['model'],'owner-model');self.assertEqual(project['effort'],'high')
        self.assertTrue(project['local_control'])

    def test_local_console_rejects_foreign_host_and_forged_csrf(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.connector,0));port=server.server_port
        server.RequestHandlerClass=handler_for(self.connector,port)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f'http://127.0.0.1:{port}'
        with urllib.request.urlopen(url+'/api/state') as response:body=response.read().decode()
        self.assertNotIn('private-device-token-value',body)
        for headers in ({'Host':'attacker.example'},{'Origin':url,'X-CSRF':'forged'}):
            request=urllib.request.Request(url+('/api/state' if 'Host' in headers else '/api/action'),headers=headers,
                data=None if 'Host' in headers else b'{"action":"approve"}')
            with self.assertRaises(urllib.error.HTTPError) as result:urllib.request.urlopen(request)
            self.assertEqual(result.exception.code,403)


if __name__=='__main__':unittest.main()
