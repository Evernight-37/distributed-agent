import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from collaboration_store import Store
from common import read_json, write_json

# These tests isolate catalog/control behavior. Signature cryptography is exercised
# by account_identity's own suite; no production verification is replaced.
def verify(envelope, expected_account):
    if envelope.get('signed_by')!=expected_account: raise PermissionError('bad signature')

def load(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).resolve().parents[1]/'src'/'agent_harness'/(name+'.py'))
    module=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,{'account_identity':types.SimpleNamespace(verify_owner_command=verify)}): spec.loader.exec_module(module)
    return module
backend=load('owner_backend')
with patch.dict(sys.modules,{'owner_backend':backend}): worker=load('owner_worker')

class FakeConnector:
    def __init__(self,root):
        self.root=Path(root);self.config={'owner_account_id':'u1','node':'n1'};self.saved=[];self.calls=[]
    def bindings(self):return self.saved[:]
    def save_binding(self,b):
        self.saved=[x for x in self.saved if x['backend_project']!=b['backend_project']]+[dict(b)];return b
    def executor(self,r):
        self.calls.append(r)
        if r['action']=='register':write_json(self.root/'projects'/r['project']['name']/'project.json',r['project'])
        elif r['action']=='configure':
            path=self.root/'projects'/r['project']/'project.json';p=read_json(path);p.update({k:r[k] for k in ('model','effort','timeout')});write_json(path,p)
        return {}

class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'store');self.backend=backend.OwnerBackend(self.store)
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES('u1','one','s','p')");db.execute("INSERT INTO users VALUES('u2','two','s','p')")
            db.execute("INSERT INTO nodes VALUES('n1','u1')");db.execute("INSERT INTO nodes VALUES('n2','u2')")
            db.execute("INSERT INTO projects(id,name,owner_id) VALUES('p1','One','u1')");db.execute("INSERT INTO project_users VALUES('p1','u1')")
        self.connector=FakeConnector(self.root/'device');self.control=worker.OwnerControl(self.connector);self.counter=0
        self.workspace=self.root/'workspace';self.workspace.mkdir()
    def tearDown(self):self.tmp.cleanup()
    def envelope(self,action,data,target='n1',owner='u1',cid=None):
        self.counter+=1
        return {'signed_by':owner,'body':{'domain':'harness-owner-command-v1','account_id':owner,'target':target,'id':cid or str(self.counter),'action':action,'data':data}}
    def create(self):
        return self.control.apply(self.envelope('create',{'id':'e1','name':'Employee'}))
    def configure(self):
        return self.control.apply(self.envelope('configure',{'id':'e1','settings':{'remote_path':str(self.workspace),'model':'gpt-5.6-sol','effort':'high','enabled':True}}))
    def test_ownership_and_ack(self):
        env=self.envelope('create',{'id':'e1','name':'Employee'})
        self.backend.enqueue('u1',env)
        self.assertEqual(self.backend.snapshot('u1')['employees'][0]['settings'],{})
        self.assertEqual(self.backend.snapshot('u2')['employees'],[])
        with self.assertRaises(PermissionError):self.backend.enqueue('u1',self.envelope('browse',{},target='n2'))
        with self.store.connect() as db:
            self.assertEqual(len(self.backend.poll(db,{'node':'n1','owner_id':'u1'})),1)
            with self.assertRaises(PermissionError):self.backend.report(db,{'node':'n2','owner_id':'u2'},env['body']['id'],'applied',{})
            result={'settings':{'enabled':False},'name':'Employee'}
            self.backend.report(db,{'node':'n1','owner_id':'u1'},env['body']['id'],'applied',result)
            self.assertTrue(self.backend.report(db,{'node':'n1','owner_id':'u1'},env['body']['id'],'applied',result)['duplicate'])
            with self.assertRaises(ValueError):self.backend.report(db,{'node':'n1','owner_id':'u1'},env['body']['id'],'failed',{})
        self.assertEqual(self.backend.snapshot('u1')['employees'][0]['revision'],1)
    def test_membership_and_no_execution_settings(self):
        self.backend.enqueue('u1',self.envelope('create',{'id':'e1','name':'Employee'}))
        with self.assertRaises(PermissionError):
            self.backend.enqueue('u1',self.envelope('dispatch',{'id':'e1','project_id':'unknown'}))
        self.create()
        response=self.control.apply(self.envelope('configure',{'id':'e1','settings':{'shell':'do anything'}}))
        self.assertEqual(response['state'],'failed');self.assertEqual(self.connector.calls,[])

    def test_idempotency(self):
        env=self.envelope('create',{'id':'e1','name':'Employee'},cid='repeat')
        self.backend.enqueue('u1',env);self.assertTrue(self.backend.enqueue('u1',env)['duplicate'])
        changed=self.envelope('create',{'id':'e1','name':'Different'},cid='repeat')
        with self.assertRaises(ValueError):self.backend.enqueue('u1',changed)
        response=self.control.apply(env);self.assertEqual(response,self.control.apply(env))
        with self.assertRaises(ValueError):self.control.apply(changed)
    def test_signature_target(self):
        with self.assertRaises(PermissionError):self.control.apply(self.envelope('browse',{},target='n2'))
        with self.assertRaises(PermissionError):self.control.apply(self.envelope('browse',{},owner='u2'))
    def test_sessions_and_withdraw(self):
        self.create();self.assertEqual(self.configure()['state'],'applied')
        first=self.control.apply(self.envelope('dispatch',{'id':'e1','project_id':'p1'}));second=self.control.apply(self.envelope('dispatch',{'id':'e1','project_id':'p2'}))
        self.assertEqual(first['state'],'applied');self.assertEqual(second['state'],'applied');self.assertNotEqual(first['result']['backend_project'],second['result']['backend_project'])
        backend_name=first['result']['backend_project'];write_json(self.connector.root/'projects'/backend_name/'active.json',{'job':'running'})
        self.assertEqual(self.configure()['state'],'failed')
        withdrawn=self.control.apply(self.envelope('withdraw',{'id':'e1','project_id':'p1'}));self.assertEqual(withdrawn['state'],'applied')
        self.assertTrue(read_json(self.connector.root/'projects'/backend_name/'active.json'))
    def test_browse_and_unknown_receipt(self):
        (self.workspace/'folder').mkdir();(self.workspace/'secret.txt').write_text('not disclosed')
        response=self.control.apply(self.envelope('browse',{'path':str(self.workspace)}))
        self.assertEqual(response['result']['children'],[{'name':'folder','path':str(self.workspace/'folder')}])
        env=self.envelope('browse',{'path':str(self.workspace)})
        receipt=self.connector.root/'owner-receipts'/(worker.key(env['body']['id'])+'.json')
        write_json(receipt,{'fingerprint':worker.key(worker.canonical(env['body'])),'phase':'started'})
        self.assertEqual(self.control.apply(env)['state'],'failed')

if __name__=='__main__':unittest.main()
