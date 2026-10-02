"""Service hosting belongs to an account, not to every visiting project member."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from account_backend import AccountBackend, device_node
from account_identity import approve_certificate, generate_identity, sign_envelope
from broker import PullBackend
from collaboration_store import Store
from app_bridge import AppBridge


class ServiceHostingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.api=AccountBackend(self.store,PullBackend(self.store))

    def tearDown(self):
        self.temp.cleanup()

    def call(self,identity,operation,payload,action='rpc'):
        challenge=self.api.api({'action':'challenge','public_key':identity['public_key']})
        envelope=sign_envelope(identity,{'domain':'harness-account-request-v1',
            'nonce':challenge['nonce'],'operation':operation,'payload':payload})
        return self.api.api({'action':action,'envelope':envelope})

    def test_unclaimed_legacy_service_denies_creation_until_local_claim(self):
        owner=generate_identity()
        self.call(owner,'register',{'name':'Owner'},'register')
        with self.assertRaises(PermissionError):
            self.call(owner,'create-project',{'name':'Before claim'})
        self.store.claim_local_host(owner['account_id'],device_node(owner['public_key']))
        with self.assertRaises(PermissionError):
            self.store.claim_local_host(generate_identity()['account_id'])
        self.assertTrue(self.call(owner,'dashboard',{})['hosted_by_me'])
        self.assertEqual(self.call(owner,'create-project',{'name':'After claim'})['name'],'After claim')

    def test_linked_child_can_host_but_visiting_account_cannot_create(self):
        root=generate_identity()
        child=generate_identity()
        child['chain']=approve_certificate(root,child['public_key'])
        child['account_id']=root['account_id']
        self.store.claim_local_host(root['account_id'],device_node(child['public_key']))
        self.call(child,'register',{'name':'Owner','device_name':'Second computer'},'register')
        self.assertTrue(self.call(child,'dashboard',{})['hosted_by_me'])
        self.assertEqual(self.call(child,'create-project',{'name':'Hosted on second computer'})['name'],
                         'Hosted on second computer')
        self.call(root,'activate-device',{'device_name':'First computer'},'activate-device')
        self.assertTrue(self.call(root,'dashboard',{})['hosted_by_me'])
        visitor=generate_identity()
        self.call(visitor,'register',{'name':'Visitor'},'register')
        self.assertFalse(self.call(visitor,'dashboard',{})['hosted_by_me'])
        with self.assertRaises(PermissionError):
            self.call(visitor,'create-project',{'name':'Unauthorized'})
        with self.assertRaises(ValueError):
            self.call(visitor,'claim-local-host',{'account_id':visitor['account_id']})

    def test_linked_device_desktop_enables_its_own_local_service(self):
        root=generate_identity()
        child=generate_identity()
        child['chain']=approve_certificate(root,child['public_key'])
        child['account_id']=root['account_id']
        bridge=AppBridge(Path(self.temp.name)/'desktop')
        bridge.identity=child
        bridge.profile={'id':'local','username':'Owner','server':'https://primary.example',
                        'node':device_node(child['public_key'])}
        with patch.object(bridge,'_server',return_value='http://127.0.0.1:8765'), \
             patch.object(bridge,'_local_host_state',return_value=self.store), \
             patch.object(bridge,'_post',side_effect=lambda _server,request:self.api.api(request)), \
             patch.object(bridge,'_save_public_profile'),patch.object(bridge,'_enroll'):
            result=bridge._enable_local_host()
        self.assertTrue(result['enabled'])
        self.assertEqual(self.store.host_owner()['owner_account_id'],root['account_id'])
        self.assertTrue(self.call(child,'dashboard',{})['hosted_by_me'])

    def test_local_host_claim_requires_matching_running_data_directory(self):
        identity=generate_identity()
        bridge=AppBridge(Path(self.temp.name)/'desktop')
        bridge.identity=identity
        with patch.object(bridge,'_post',return_value={'service_id':self.store.service_id()}):
            self.assertEqual(bridge._local_host_state(self.temp.name).service_id(),self.store.service_id())
        with patch.object(bridge,'_post',return_value={'service_id':'another-service'}):
            with self.assertRaisesRegex(ValueError,'其它数据目录'):
                bridge._local_host_state(self.temp.name)


if __name__=='__main__':
    unittest.main()
