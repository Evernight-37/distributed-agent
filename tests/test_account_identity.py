import copy
import tempfile
import unittest
from pathlib import Path
from account_identity import generate_identity,sign_envelope,verify_envelope,verify_owner_command,approve_certificate,encrypt_identity,decrypt_identity
from account_backend import AccountBackend
from collaboration_store import Store
from broker import PullBackend

class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name));self.api=AccountBackend(self.store,PullBackend(self.store));self.root=generate_identity()
        self.store.claim_local_host(self.root['account_id'])
    def tearDown(self):self.temp.cleanup()
    def call(self,identity,operation,payload,action='rpc'):
        nonce=self.api.api({'action':'challenge','public_key':identity['public_key']})['nonce']
        env=sign_envelope(identity,{'domain':'harness-account-request-v1','nonce':nonce,'operation':operation,'payload':payload})
        return self.api.api({'action':action,'envelope':env})
    def register(self,identity=None):return self.call(identity or self.root,'register',{'name':'Test','device_name':'Laptop'},'register')
    def test_encryption(self):
        blob=encrypt_identity(self.root,'');self.assertNotIn(self.root['private_key'],str(blob));self.assertEqual(decrypt_identity(blob,''),self.root)
        with self.assertRaises(PermissionError):decrypt_identity(blob,'wrong')
        bad=copy.deepcopy(blob);bad['ciphertext']=bad['ciphertext'][:-5]+'AAAAA'
        with self.assertRaises(PermissionError):decrypt_identity(bad,'')
    def test_signature_and_chain(self):
        child=generate_identity();child['chain']=approve_certificate(self.root,child['public_key']);child['account_id']=self.root['account_id']
        body={'domain':'harness-owner-command-v1','account_id':self.root['account_id'],'id':'c1','target':'node'};env=sign_envelope(child,body)
        self.assertEqual(verify_owner_command(env,self.root['account_id']),body)
        other=generate_identity()
        with self.assertRaises(PermissionError):verify_owner_command(env,other['account_id'])
        bad=copy.deepcopy(env);bad['chain'][0]['body']['child']=other['public_key']
        with self.assertRaises(PermissionError):verify_envelope(bad)
        bad=copy.deepcopy(env);bad['body']['target']='different'
        with self.assertRaises(PermissionError):verify_envelope(bad)
    def test_replay_and_no_legacy_login(self):
        nonce=self.api.api({'action':'challenge','public_key':self.root['public_key']})['nonce']
        env=sign_envelope(self.root,{'domain':'harness-account-request-v1','nonce':nonce,'operation':'register','payload':{'name':'Test'}})
        self.api.api({'action':'register','envelope':env})
        with self.assertRaises(PermissionError):self.api.api({'action':'register','envelope':env})
        result=self.call(self.root,'dashboard',{});self.assertEqual(result['employees'],[])
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM users WHERE id=?',(self.root['account_id'],)).fetchone();self.assertEqual(len(row['password_hash']),128)
    def test_existing_root_identity_accepts_invite_on_another_service(self):
        with tempfile.TemporaryDirectory() as other_dir:
            other_store=Store(Path(other_dir));other=AccountBackend(other_store,PullBackend(other_store))
            host=generate_identity()
            other_store.claim_local_host(host['account_id'])
            def invoke(identity,operation,payload,action):
                nonce=other.api({'action':'challenge','public_key':identity['public_key']})['nonce']
                envelope=sign_envelope(identity,{'domain':'harness-account-request-v1','nonce':nonce,'operation':operation,'payload':payload})
                return other.api({'action':action,'envelope':envelope})
            invoke(host,'register',{'name':'Host'},'register')
            project=invoke(host,'create-project',{'name':'Shared'},'rpc')['id']
            invite=invoke(host,'invite',{'project':project},'rpc')['invite']
            payload={'invite':invite,'name':'My existing identity','device_name':'My laptop'}
            result=invoke(self.root,'accept-project-invite',payload,'accept-project-invite')
            self.assertEqual((result['account_id'],result['project']),(self.root['account_id'],project))
            view=invoke(self.root,'project',{'id':project},'rpc')
            self.assertEqual(view['project']['id'],project)
            with self.assertRaises(PermissionError):invoke(self.root,'accept-project-invite',payload,'accept-project-invite')
            self.assertEqual(invoke(self.root,'dashboard',{},'rpc')['user']['id'],self.root['account_id'])

    def test_child_identity_can_join_another_service_with_same_account_id(self):
        with tempfile.TemporaryDirectory() as other_dir:
            other_store=Store(Path(other_dir));other=AccountBackend(other_store,PullBackend(other_store))
            host=generate_identity()
            other_store.claim_local_host(host['account_id'])
            def invoke(identity,operation,payload,action):
                nonce=other.api({'action':'challenge','public_key':identity['public_key']})['nonce']
                return other.api({'action':action,'envelope':sign_envelope(identity,{'domain':'harness-account-request-v1','nonce':nonce,'operation':operation,'payload':payload})})
            invoke(host,'register',{'name':'Host'},'register')
            project=invoke(host,'create-project',{'name':'Shared'},'rpc')['id']
            invite=invoke(host,'invite',{'project':project},'rpc')['invite']
            child=generate_identity();child['chain']=approve_certificate(self.root,child['public_key']);child['account_id']=self.root['account_id']
            result=invoke(child,'accept-project-invite',{'invite':invite,'name':'Existing','device_name':'Second computer'},'accept-project-invite')
            self.assertEqual(result['account_id'],self.root['account_id'])
            self.assertEqual(invoke(child,'dashboard',{},'rpc')['user']['id'],self.root['account_id'])
            with other_store.connect() as db:
                rows=db.execute('SELECT public_key FROM account_devices WHERE account_id=?',(self.root['account_id'],)).fetchall()
            self.assertEqual({row['public_key'] for row in rows},{self.root['public_key'],child['public_key']})
    def test_device_approval_and_revocation(self):
        self.register();child=generate_identity()
        request=self.api.api({'action':'request-device','public_key':child['public_key'],'name':'Desktop'})
        self.assertEqual(self.api.api({'action':'claim-device',**request})['state'],'pending')
        details=self.call(self.root,'inspect-device',{'code':request['code']});self.assertEqual(details['public_key'],child['public_key'])
        chain=approve_certificate(self.root,child['public_key'])
        self.call(self.root,'approve-device',{'request_id':request['request_id'],'chain':chain})
        claim=self.api.api({'action':'claim-device',**request});child.update(account_id=claim['account_id'],chain=claim['chain'])
        self.assertEqual(len(self.call(child,'dashboard',{})['devices']),2)
        self.call(self.root,'revoke-device',{'public_key':child['public_key']})
        with self.assertRaises(PermissionError):self.call(child,'dashboard',{})
        with self.assertRaises(PermissionError):self.call(child,'activate-device',{'device_name':'Desktop'},'activate-device')
    def approve_child(self,parent):
        child=generate_identity();request=self.api.api({'action':'request-device','public_key':child['public_key'],'name':'Child'})
        self.call(parent,'approve-device',{'request_id':request['request_id'],'chain':approve_certificate(parent,child['public_key'])})
        claim=self.api.api({'action':'claim-device',**request});child.update(account_id=claim['account_id'],chain=claim['chain'])
        return child
    def test_root_revoke_and_descendant_cascade(self):
        from account_backend import device_node
        from collaboration_store import digest
        self.register();child=self.approve_child(self.root);grandchild=self.approve_child(child)
        self.assertTrue(next(d for d in self.call(self.root,'dashboard',{})['devices'] if d['public_key']==self.root['public_key'])['is_root'])
        with self.assertRaises(ValueError):self.call(child,'revoke-device',{'id':device_node(self.root['public_key'])})
        with self.store.connect() as db:
            db.execute('INSERT INTO pull_devices VALUES(?,?,?)',(device_node(grandchild['public_key']),digest('synthetic-token-only'),0))
        command=sign_envelope(self.root,{'domain':'harness-owner-command-v1','account_id':self.root['account_id'],'id':'cascade-command','target':device_node(grandchild['public_key']),'action':'browse','data':{}})
        self.call(self.root,'owner-command',{'envelope':command})
        self.call(self.root,'revoke-device',{'id':device_node(child['public_key'])})
        with self.assertRaises(PermissionError):self.call(grandchild,'dashboard',{})
        with self.store.connect() as db:
            self.assertFalse(db.execute('SELECT 1 FROM pull_devices WHERE node=?',(device_node(grandchild['public_key']),)).fetchone())
            self.assertEqual(db.execute("SELECT state FROM owner_commands WHERE id='cascade-command'").fetchone()['state'],'failed')
        self.assertEqual(len(self.call(self.root,'dashboard',{})['devices']),1)
    def test_withdraw_filters_without_deleting_history(self):
        import json
        from account_backend import device_node
        from owner_worker import execution_project
        self.register();project=self.call(self.root,'create-project',{'name':'Project'})['id'];user=self.root['account_id'];node=device_node(self.root['public_key']);backend=execution_project('employee',project)
        with self.store.connect() as db:
            db.execute('INSERT INTO employees VALUES(?,?,?,?,?,?,?)',('employee','Employee',user,node,'{}','idle',0))
            db.execute('INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)',('agent',project,'Employee',user,node,backend,'model','medium',120))
            body={'action':'withdraw','data':{'id':'employee','project_id':project}}
            db.execute('INSERT INTO owner_commands VALUES(?,?,?,?,?,?,?)',('withdraw',user,node,json.dumps({'body':body}),'applied','{}',1))
        self.assertEqual(self.call(self.root,'project',{'id':project})['members'],[])
        with self.store.connect() as db:self.assertIsNotNone(db.execute("SELECT * FROM agents WHERE id='agent'").fetchone())

    def test_cross_account_approval(self):
        self.register();other=generate_identity();self.register(other);child=generate_identity()
        request=self.api.api({'action':'request-device','public_key':child['public_key'],'name':'Desktop'})
        with self.assertRaises(PermissionError):self.call(other,'approve-device',{'request_id':request['request_id'],'chain':approve_certificate(self.root,child['public_key'])})

if __name__=='__main__':unittest.main()
