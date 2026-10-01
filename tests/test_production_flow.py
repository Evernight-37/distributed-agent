import tempfile,threading,unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from panel import handler_for
from app_bridge import AppBridge
from pull_worker import enroll,Connector
from common import read_json,write_json

class ProductionFlow(unittest.TestCase):
    def test_local_login_restarts_local_service_after_complete_exit(self):
        from account_identity import generate_identity, encrypt_identity
        from desktop_runtime import DesktopRuntime
        with tempfile.TemporaryDirectory() as td:
            bridge=AppBridge(Path(td))
            profile={'id':'saved-local','username':'本机用户','server':'http://127.0.0.1:8765',
                     'encrypted':encrypt_identity(generate_identity(),'password')}
            with patch.object(bridge,'_profiles',return_value=[profile]), patch.object(DesktopRuntime,'start_workbench',return_value='http://127.0.0.1:8765') as start, patch.object(bridge,'_rpc',return_value={}), patch.object(bridge,'_enroll'):
                result=bridge.invoke('login',{'profile':'saved-local','password':'password'})
            self.assertTrue(result['ok'],result)
            start.assert_called_once_with(open_browser=False)

    def test_two_devices_and_owner_controls(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);handler=handler_for(root/'service');server=ThreadingHTTPServer(('127.0.0.1',0),handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();url='http://127.0.0.1:'+str(server.server_port)
            class Bridge(AppBridge):
                def _enroll(self):
                    runtime=self._runtime()
                    if not runtime.config_path.exists():
                        pair=self._rpc('pair-local',{'node':self.profile['node']})
                        enroll(self.profile['server'],pair.get('code',pair.get('pair_code')),runtime.root)
                        config=read_json(runtime.config_path);config['owner_account_id']=self.identity['account_id'];write_json(runtime.config_path,config)
            a=Bridge(root/'a');b=Bridge(root/'b')
            def call(client,op,data=None):
                result=client.invoke(op,data);self.assertTrue(result['ok'],result);return result['result']
            try:
                call(a,'register',{'username':'测试账户','password':'','server':url})
                dash=call(a,'dashboard');self.assertEqual(len(dash['devices']),1)
                req=call(b,'device-request',{'name':'第二台','password':'短','server':url})
                call(a,'device-inspect',{'code':req['code']});call(a,'device-approve',{'request_id':req['request_id']})
                call(b,'device-claim',{'request_id':req['request_id']})
                self.assertEqual(a.identity['account_id'],b.identity['account_id']);self.assertNotEqual(a.identity['private_key'],b.identity['private_key'])
                project=call(a,'new-project',{'name':'独立测试项目'});pid=project.get('id',project.get('project',{}).get('id'))
                queued=call(a,'employee-create',{'name':'第二台员工','node':b.profile['node']});connector=Connector(b._runtime().config_path);connector.cycle()
                employees=call(a,'dashboard')['employees'];self.assertEqual(len(employees),1);employee=employees[0]['id']
                work=root/'project';work.mkdir()
                call(a,'employee-configure',{'id':employee,'name':'第二台员工','model':'gpt-5.6-sol','effort':'medium','path':str(work),'enabled':False,'stages':['discuss'],'timeout':600});connector.cycle()
                call(a,'employee-dispatch',{'id':employee,'project':pid});connector.cycle();connector.cycle()
                view=call(a,'project',{'id':pid,'visit':True});self.assertEqual(view['members'][0]['employee_id'],employee)
                call(b,'message',{'project':pid,'body':'跨设备消息'});self.assertEqual(call(a,'project',{'id':pid})['messages'][-1]['body'],'跨设备消息')
                call(a,'employee-withdraw',{'id':employee,'project':pid});connector.cycle();connector.cycle()
                self.assertEqual(call(a,'project',{'id':pid})['members'],[])
                call(a,'employee-configure',{'id':employee,'name':'第二台员工','model':'gpt-5.6-sol','effort':'medium','path':str(work),'enabled':True,'stages':['discuss'],'timeout':600});connector.cycle();connector.cycle()
                self.assertFalse(connector.bindings()[0]['enabled'])
                call(a,'employee-configure',{'id':employee,'name':'第二台员工','model':'gpt-5.6-sol','effort':'medium','path':str(root/'missing'),'enabled':True,'stages':['discuss'],'timeout':600});connector.cycle()
                self.assertIn('does not exist',call(a,'dashboard')['employees'][0]['last_error'])
                call(a,'device-revoke',{'id':b.profile['node']});self.assertFalse(b.invoke('dashboard')['ok'])
                with self.assertRaises(Exception):connector.cycle()
                profile=a.profile['id'];call(a,'logout');call(a,'login',{'profile':profile,'password':''})
            finally:server.shutdown();server.server_close();thread.join()
if __name__=='__main__':unittest.main()
