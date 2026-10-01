"""Trusted native bridge. Local unlock passwords never leave this process."""
import json
import hashlib
import os
import secrets
import socket
import threading
import urllib.request
import urllib.error
from pathlib import Path
from common import read_json, write_json
from desktop_runtime import DesktopRuntime
from pull_worker import validate_server, NoRedirect
from account_identity import generate_identity, sign_envelope, encrypt_identity, decrypt_identity, approve_certificate

class AppBridge:
    def __init__(self, root=None):
        self.root=Path(root or Path(os.environ.get('LOCALAPPDATA',Path.home()))/'AgentHarness'/'app-v5').resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.identity=None;self.profile=None;self.window=None;self.pending=None;self.inspected={}
        self.guard=threading.RLock()
        self.allow_window_close=False
    def _profiles(self):
        return [read_json(p) for p in (self.root/'profiles').glob('*.json')]
    def _post(self,server,data):
        validate_server(server)
        req=urllib.request.Request(server.rstrip('/')+'/api/account',data=json.dumps(data,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(req,timeout=15) as response: result=json.load(response)
        except urllib.error.HTTPError as exc:
            try: message=json.load(exc).get('error','服务拒绝请求')
            except Exception: message='服务拒绝请求'
            raise RuntimeError(message) from None
        if 'error' in result: raise RuntimeError(result['error'])
        return result.get('result',result)
    def _server(self,server):
        server=(server or 'http://127.0.0.1:8765').rstrip('/')
        validate_server(server)
        if server=='http://127.0.0.1:8765': DesktopRuntime(self.root/'host').start_workbench(open_browser=False)
        return server
    def _rpc(self,operation,payload=None,server=None):
        if not self.identity: raise PermissionError('请先解锁账户')
        server=server or self.profile['server']; challenge=self._post(server,{'action':'challenge','public_key':self.identity['public_key']})
        envelope=sign_envelope(self.identity,{'domain':'harness-account-request-v1','nonce':challenge['nonce'],'operation':operation,'payload':payload or {}})
        return self._post(server,{'action':'rpc','envelope':envelope})
    def _save_profile(self,password):
        self.profile['encrypted']=encrypt_identity(self.identity,password)
        path=self.root/'profiles'/(self.profile['id']+'.json');write_json(path,self.profile)
        self._secure(path)
    def _secure(self,path):
        if os.name=='nt':
            import subprocess,csv
            who=subprocess.check_output(['whoami.exe','/user','/fo','csv','/nh'],text=True,encoding='utf-8',errors='replace')
            sid=next(csv.reader(who.splitlines()))[-1]
            proc=subprocess.run(['icacls.exe',str(path),'/inheritance:r','/grant:r','*'+sid+':F','*S-1-5-18:F'],capture_output=True,timeout=15)
            if proc.returncode: raise RuntimeError('无法限制本地账户文件权限')
        else: path.chmod(0o600)
    def _runtime(self):
        if not self.profile: raise PermissionError('请先登录')
        return DesktopRuntime(self.root/'receivers'/self.profile['node'])
    def _enroll(self):
        runtime=self._runtime()
        if not runtime.config_path.exists():
            pair=self._rpc('pair-local',{'node':self.profile['node']})
            runtime.pair(self.profile['server'],pair.get('code',pair.get('pair_code')))
            config=read_json(runtime.config_path);config['owner_account_id']=self.identity['account_id'];write_json(runtime.config_path,config);self._secure(runtime.config_path)
        runtime.start()
    def _owner(self,action,node,data):
        envelope=sign_envelope(self.identity,{'domain':'harness-owner-command-v1','account_id':self.identity['account_id'],'target':node,'id':'command-'+secrets.token_hex(16),'action':action,'data':data})
        return self._rpc('owner-command',{'envelope':envelope})
    def _employee(self,eid):
        data=self._rpc('dashboard')
        return next((e for e in data['employees'] if e['id']==eid),None) or self._missing()
    def _missing(self): raise ValueError('员工不存在')
    def invoke(self,action,data=None):
        try:
            with self.guard: return {'ok':True,'result':self._invoke(action,data or {})}
        except Exception as exc: return {'ok':False,'error':str(exc)}
    def _invoke(self,action,data):
        if action=='app-services':
            from app_lifecycle import Lifecycle
            return Lifecycle(self.root).status()
        if action in {'app-exit','app-background'}:
            if action=='app-exit':
                from app_lifecycle import Lifecycle
                result=Lifecycle(self.root).stop()
            else: result={'background':True}
            self.allow_window_close=True
            if self.window:
                threading.Timer(.3,self.window.destroy).start()
            return result
        if action=='local-status': return {'profiles':[{'id':p['id'],'username':p['username']} for p in self._profiles()],'unlocked':bool(self.identity),'server':(self.profile or {}).get('server','http://127.0.0.1:8765')}
        if action=='register':
            identity=generate_identity();server=self._server(data.get('server'))
            challenge=self._post(server,{'action':'challenge','public_key':identity['public_key']})
            body={'domain':'harness-account-request-v1','nonce':challenge['nonce'],'operation':'register','payload':{'name':data['username'],'device_name':socket.gethostname()}}
            self._post(server,{'action':'register','envelope':sign_envelope(identity,body)})
            self.identity=identity;self.profile={'id':secrets.token_hex(16),'username':data['username'],'server':server,'node':'device-'+hashlib.sha256(identity['public_key'].encode()).hexdigest()[:24]}
            self._save_profile(data.get('password',''));self._enroll();return {'created':True}
        if action=='login':
            profile=next((p for p in self._profiles() if p['id']==data['profile']),None)
            if not profile: raise ValueError('本地账户不存在')
            identity=decrypt_identity(profile['encrypted'],data.get('password',''))
            self.identity=identity;self.profile=profile
            self._rpc('dashboard');self._enroll();return {'unlocked':True}
        if action=='logout': self.identity=None;self.profile=None;return {}
        if action=='device-request':
            identity=generate_identity();server=self._server(data.get('server'));name=data.get('name') or socket.gethostname()
            result=self._post(server,{'action':'request-device','public_key':identity['public_key'],'name':name})
            self.pending={'identity':identity,'server':server,'password':data.get('password',''),'request':result}
            return result
        if action=='device-claim':
            if not self.pending: raise ValueError('本机没有待关联请求')
            pending=self.pending;r=self._post(pending['server'],dict(action='claim-device',**pending['request']))
            if r.get('status')!='approved': return r
            identity=pending['identity'];identity.update(account_id=r['account_id'],chain=r['chain'])
            from account_identity import verify_envelope
            verify_envelope(sign_envelope(identity,{'claim':True}),expected_account=r['account_id'])
            self.identity=identity;self.profile={'id':secrets.token_hex(16),'username':r['username'],'server':pending['server'],'node':'device-'+hashlib.sha256(identity['public_key'].encode()).hexdigest()[:24]}
            self._save_profile(pending['password']);self.pending=None;self._enroll();return {'status':'approved'}
        if not self.identity: raise PermissionError('请先登录')
        if action=='dashboard':
            result=self._rpc('dashboard')
            for d in result.get('devices',[]): d['local']=(d.get('node',d.get('id'))==self.profile['node'])
            for employee in result.get('employees',[]):
                if not employee.get('pending'): employee['pending']=None
            result['pending_devices']=list(self.inspected.values())
            result['pending_approvals']=[]
            for receipt in (self._runtime().root/'pull-receipts').glob('*.json'):
                value=read_json(receipt,{})
                if value.get('phase')=='waiting_local_approval':
                    job=value['job'];backend=job['backend_project'];template_name=job['request'].get('template');template=read_json(self._runtime().root/'projects'/backend/'execution_templates.json',{}).get(template_name)
                    project=read_json(self._runtime().root/'projects'/backend/'project.json',{})
                    result['pending_approvals'].append({'job_id':job['id'],'template':template_name,'expected_template':template,'argv':(template or {}).get('argv'),'cwd':project.get('remote_path'),'node':self.profile['node']})
            return result
        if action=='device-inspect':
            request=self._rpc('inspect-device',{'code':data['code']});request['public_key_fingerprint']=hashlib.sha256(request['public_key'].encode()).hexdigest()[:16];self.inspected[request['id']]=request
            return request
        if action=='device-approve':
            request=self.inspected.get(data['request_id'])
            if not request: raise ValueError('关联请求不存在')
            chain=approve_certificate(self.identity,request['public_key'])
            result=self._rpc('approve-device',{'request_id':request['id'],'chain':chain});self.inspected.pop(request['id'],None);return result
        if action=='device-revoke': return self._rpc('revoke-device',data)
        if action=='employee-create': return self._owner('create',data['node'],{'id':'employee-'+secrets.token_hex(16),'name':data['name']})
        if action=='employee-configure':
            employee=self._employee(data['id']);settings={k:data[k] for k in ('model','effort','enabled','stages','timeout')};settings['remote_path']=data['path']
            return self._owner('configure',employee['node'],{'id':data['id'],'name':data['name'],'settings':settings})
        if action in {'employee-dispatch','employee-withdraw'}:
            employee=self._employee(data['id']);return self._owner('dispatch' if action.endswith('dispatch') else 'withdraw',employee['node'],{'id':data['id'],'project_id':data['project']})
        if action=='browse-folders':
            r=self._owner('browse',data['node'],{'path':data.get('path')});return {'request_id':r['id']}
        if action=='command-status': return self._rpc('command-status',data)
        if action=='choose-folder':
            if not self.window: raise RuntimeError('文件夹选择器尚未就绪')
            import webview
            chosen=self.window.create_file_dialog(webview.FileDialog.FOLDER)
            return {'path':chosen[0] if chosen else ''}
        if action=='new-project': return self._rpc('create-project',data)
        if action in {'project','join-project','invite','message','set-admin'}:
            result=self._rpc(action,data)
            if action=='invite':
                from team_service import TeamService
                entry=TeamService(self.root,self.profile['server']).status()
                result['server']=entry.get('url') if entry['state']=='ready' else self.profile['server']
            return result
        if action=='receiver-start': self._enroll();return self._runtime().status()
        if action=='receiver-stop': return self._runtime().stop_and_wait()
        if action=='approve-execution': return self._runtime()._control('action',dict(action='approve-execution',**data))
        if action=='settings-get':
            from team_service import TeamService
            return {'server':self.profile['server'],'background':self.profile.get('background',True),'autostart':False,'data_root':str(self.root),'team_service':TeamService(self.root,self.profile['server']).status()}
        if action.startswith('team-service-'):
            from team_service import TeamService
            service=TeamService(self.root,self.profile['server'])
            if action=='team-service-status':return service.status()
            if action=='team-service-create':
                status=service.status()
                if not status.get('can_manage'):raise PermissionError('请在托管协作服务的电脑创建入口。')
                self._rpc('dashboard')
                return service.create()
            if action=='team-service-stop':return service.stop()
            if action=='team-service-authorize':return service.authorize()
            if action=='team-service-copy':return service.copy()
            if action=='team-service-test':
                status=service.status()
                if status['state'] not in {'ready','remote'}:raise ValueError(status.get('message','入口尚未配置'))
                self._rpc('dashboard',server=status['url'])
                return {'ok':True,'message':'连接成功：当前账户可通过此地址访问协作服务。'}
        if action=='settings-save':
            if 'server' in data and data['server'].rstrip('/')!=self.profile['server']: raise ValueError('账户与项目绑定当前服务。更换服务请创建或关联该服务的账户，避免接收端连接错位。')
            if 'background' in data:self.profile['background']=bool(data['background']);write_json(self.root/'profiles'/(self.profile['id']+'.json'),self.profile)
            return {'saved':True}
        if action=='import-legacy': raise ValueError('旧版账户与会话不会自动合并。请继续使用旧版入口；迁移工具将在核对项目映射后提供。')
        raise ValueError('未知应用操作')
    def close(self):
        # All service operations happen before the user-approved window close.
        # The native closed event must never block on network/process cleanup.
        self.identity=None;self.pending=None
