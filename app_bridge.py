"""Trusted native bridge. Local unlock passwords never leave this process."""
import json
import hashlib
import os
import secrets
import socket
import time
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
        self.project_servers={};self.employee_servers={};self.command_servers={};self.job_servers={}
        self.last_service_sync=0
        self.activated_services=set()
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
            if exc.code==403:raise PermissionError(message) from None
            raise RuntimeError(message) from None
        except urllib.error.URLError as exc:
            raise RuntimeError('无法连接协作服务 '+server+'。请检查该电脑的服务是否运行，以及 Tailscale 是否已连接。') from None
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
    def _save_public_profile(self):
        path=self.root/'profiles'/(self.profile['id']+'.json')
        write_json(path,self.profile);self._secure(path)
    def _services(self):
        if not self.profile:return []
        return list(dict.fromkeys([self.profile['server'],*self.profile.get('services',[])]))
    def _attached_services(self):
        return list(dict.fromkeys([self.profile['server'],*self.profile.get('attached_services',[])]))
    def _sync_service_links(self,force=False):
        if not self.profile or not self.identity:return
        if not force and time.monotonic()-self.last_service_sync<30:return
        self.last_service_sync=time.monotonic()
        try: remote=self._rpc('service-links').get('servers',[])
        except (OSError,RuntimeError,ValueError,PermissionError):return
        changed=False
        for server in remote:
            try:validate_server(server)
            except (ValueError,TypeError):continue
            if server not in self._services():self.profile.setdefault('services',[]).append(server);changed=True
        if changed:self._save_public_profile()
        for server in self._services()[1:]:
            if server not in remote:
                try:self._rpc('link-service',{'server':server})
                except (OSError,RuntimeError,ValueError):pass
    def _activate_service(self,server):
        if server in self.activated_services:return
        challenge=self._post(server,{'action':'challenge','public_key':self.identity['public_key']})
        envelope=sign_envelope(self.identity,{'domain':'harness-account-request-v1','nonce':challenge['nonce'],'operation':'activate-device','payload':{'device_name':socket.gethostname()}})
        self._post(server,{'action':'activate-device','envelope':envelope})
        self.activated_services.add(server)
    def _secure(self,path):
        if os.name=='nt':
            import subprocess,csv
            who=subprocess.check_output(['whoami.exe','/user','/fo','csv','/nh'],text=True,encoding='utf-8',errors='replace')
            sid=next(csv.reader(who.splitlines()))[-1]
            proc=subprocess.run(['icacls.exe',str(path),'/inheritance:r','/grant:r','*'+sid+':F','*S-1-5-18:F'],capture_output=True,timeout=15)
            if proc.returncode: raise RuntimeError('无法限制本地账户文件权限')
        else: path.chmod(0o600)
    def _runtime(self,server=None):
        if not self.profile: raise PermissionError('请先登录')
        server=server or self.profile['server']
        path=self.root/'receivers'/self.profile['node']
        if server!=self.profile['server']:
            path=path.with_name(path.name+'-'+hashlib.sha256(server.encode()).hexdigest()[:16])
        return DesktopRuntime(path)
    def _enroll(self,server=None):
        server=server or self.profile['server'];runtime=self._runtime(server)
        if not runtime.config_path.exists():
            pair=self._rpc('pair-local',{'node':self.profile['node']},server=server)
            runtime.pair(server,pair.get('code',pair.get('pair_code')))
            config=read_json(runtime.config_path);config['owner_account_id']=self.identity['account_id'];write_json(runtime.config_path,config);self._secure(runtime.config_path)
        runtime.start()
    def _owner(self,action,node,data,server=None):
        envelope=sign_envelope(self.identity,{'domain':'harness-owner-command-v1','account_id':self.identity['account_id'],'target':node,'id':'command-'+secrets.token_hex(16),'action':action,'data':data})
        result=self._rpc('owner-command',{'envelope':envelope},server=server)
        if isinstance(result,dict) and result.get('id'):self.command_servers[result['id']]=server or self.profile['server']
        return result
    def _employee(self,eid):
        data=self._dashboard()
        return next((e for e in data['employees'] if e['id']==eid),None) or self._missing()
    def _dashboard(self):
        self._sync_service_links()
        self.project_servers={};self.employee_servers={}
        result=None
        unavailable=[]
        denied=[]
        for server in self._services():
            try:
                self._activate_service(server)
                extra=self._rpc('dashboard',server=server)
            except PermissionError as exc:denied.append((server,str(exc)));continue
            except (OSError,RuntimeError,ValueError):unavailable.append(server);continue
            if result is None:result=extra
            else:
                result['projects'].extend(extra.get('projects',[]))
                result['employees'].extend(extra.get('employees',[]))
                result['devices'].extend(extra.get('devices',[]))
            for project in extra.get('projects',[]):
                project['server']=server;self.project_servers[project['id']]=server
            for employee in extra.get('employees',[]):
                employee['server']=server;self.employee_servers[employee['id']]=server
            for device in extra.get('devices',[]):
                device['server']=server
        if result is None and denied:raise PermissionError('账户在协作服务上被拒绝：'+denied[0][1])
        if result is None:
            result={'user':{'id':self.identity['account_id'],'username':self.profile['username']},'projects':[],'employees':[],'devices':[]}
        result['unavailable_services']=unavailable+[server for server,_ in denied]
        result['primary_server']=self.profile['server']
        result['services']=[{'server':server,'primary':server==self.profile['server'],'attached':server in self._attached_services()} for server in self._services()]
        return result
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
            if profile['server']=='http://127.0.0.1:8765':
                try:self._server(profile['server'])
                except (OSError,RuntimeError,ValueError):pass
            self.identity=identity;self.profile=profile
            self.activated_services={self.profile['server']}
            self._dashboard();self._sync_service_links(force=True)
            for server in self._attached_services():
                try:
                    self._activate_service(server)
                    self._enroll() if server==self.profile['server'] else self._enroll(server)
                except (OSError,RuntimeError,ValueError):pass
            return {'unlocked':True}
        if action=='logout': self.identity=None;self.profile=None;self.activated_services=set();return {}
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
            self.activated_services={self.profile['server']}
            self._save_profile(pending['password']);self.pending=None;self._sync_service_links(force=True);self._enroll();return {'status':'approved'}
        if not self.identity: raise PermissionError('请先登录')
        if action=='dashboard':
            result=self._dashboard();self.job_servers={}
            for d in result.get('devices',[]): d['local']=(d.get('node',d.get('id'))==self.profile['node'])
            for employee in result.get('employees',[]):
                if not employee.get('pending'): employee['pending']=None
            result['pending_devices']=list(self.inspected.values())
            result['pending_approvals']=[]
            for server in self._attached_services():
                runtime=self._runtime(server)
                for receipt in (runtime.root/'pull-receipts').glob('*.json'):
                    value=read_json(receipt,{})
                    if value.get('phase')=='waiting_local_approval':
                        job=value['job'];backend=job['backend_project'];template_name=job['request'].get('template');template=read_json(runtime.root/'projects'/backend/'execution_templates.json',{}).get(template_name)
                        project=read_json(runtime.root/'projects'/backend/'project.json',{})
                        self.job_servers[job['id']]=server
                        result['pending_approvals'].append({'job_id':job['id'],'template':template_name,'expected_template':template,'argv':(template or {}).get('argv'),'cwd':project.get('remote_path'),'node':self.profile['node'],'server':server})
            return result
        if action=='device-inspect':
            request=self._rpc('inspect-device',{'code':data['code']});request['public_key_fingerprint']=hashlib.sha256(request['public_key'].encode()).hexdigest()[:16];self.inspected[request['id']]=request
            return request
        if action=='device-approve':
            request=self.inspected.get(data['request_id'])
            if not request: raise ValueError('关联请求不存在')
            chain=approve_certificate(self.identity,request['public_key'])
            result=self._rpc('approve-device',{'request_id':request['id'],'chain':chain});self.inspected.pop(request['id'],None);return result
        if action=='device-revoke':
            server=data.get('server') or self.profile['server']
            if server not in self._services():raise ValueError('未知服务')
            return self._rpc('revoke-device',data,server=server)
        if action=='employee-create':
            server=data.get('server') or self.profile['server']
            if server not in self._services():raise ValueError('请先加入该服务上的项目')
            if data['node']==self.profile['node'] and server not in self._attached_services():raise ValueError('请先在设置中将本机 agent 接入该协作服务')
            return self._owner('create',data['node'],{'id':'employee-'+secrets.token_hex(16),'name':data['name']},server)
        if action=='employee-configure':
            employee=self._employee(data['id']);settings={k:data[k] for k in ('model','effort','enabled','stages','timeout')};settings['remote_path']=data['path']
            return self._owner('configure',employee['node'],{'id':data['id'],'name':data['name'],'settings':settings},employee['server'])
        if action in {'employee-dispatch','employee-withdraw'}:
            employee=self._employee(data['id']);project_server=self.project_servers.get(data['project'])
            if project_server!=employee['server']:raise ValueError('只能派遣已接入这个项目服务的员工')
            return self._owner('dispatch' if action.endswith('dispatch') else 'withdraw',employee['node'],{'id':data['id'],'project_id':data['project']},employee['server'])
        if action=='browse-folders':
            r=self._owner('browse',data['node'],{'path':data.get('path')},data.get('server'));return {'request_id':r['id']}
        if action=='command-status': return self._rpc('command-status',data,server=self.command_servers.get(data['id']))
        if action=='choose-folder':
            if not self.window: raise RuntimeError('文件夹选择器尚未就绪')
            import webview
            chosen=self.window.create_file_dialog(webview.FileDialog.FOLDER)
            return {'path':chosen[0] if chosen else ''}
        if action=='new-project':
            server=data.get('server') or self.profile['server']
            if server not in self._services():raise ValueError('请先加入该协作服务')
            self._activate_service(server)
            result=self._rpc('create-project',{'name':data['name']},server=server)
            self.project_servers[result['id']]=server
            return dict(result,server=server)
        if action=='join-project':
            server=self._server(data.get('server') or self.profile['server'])
            if server in self._services():result=self._rpc('join-project',{'invite':data['invite']},server=server)
            else:
                challenge=self._post(server,{'action':'challenge','public_key':self.identity['public_key']})
                payload={'invite':data['invite'],'name':self.profile['username'],'device_name':socket.gethostname()}
                envelope=sign_envelope(self.identity,{'domain':'harness-account-request-v1','nonce':challenge['nonce'],'operation':'accept-project-invite','payload':payload})
                result=self._post(server,{'action':'accept-project-invite','envelope':envelope})
                self.activated_services.add(server)
                self.profile.setdefault('services',[]).append(server);self._save_public_profile()
                self._sync_service_links(force=True)
            self.project_servers[result['project']]=server
            return dict(result,server=server)
        if action in {'project','invite','message','set-admin'}:
            project=data.get('project',data.get('id'))
            server=self.project_servers.get(project,self.profile['server'])
            result=self._rpc(action,data,server=server)
            if action=='project':result['server']=server
            if action=='invite':
                from team_service import TeamService
                entry=TeamService(self.root,server).status()
                result['server']=entry.get('url') if entry['state']=='ready' else server
            return result
        if action=='receiver-attach':
            server=self._server(data['server'])
            if server not in self._services():raise ValueError('请先接受这个服务的项目邀请')
            self._activate_service(server)
            self._rpc('dashboard',server=server)
            self._enroll(server)
            if server!=self.profile['server'] and server not in self.profile.setdefault('attached_services',[]):
                self.profile['attached_services'].append(server);self._save_public_profile()
            return {'attached':True,'server':server}
        if action=='receiver-start':
            started=[]
            for server in self._attached_services():
                self._activate_service(server)
                self._enroll() if server==self.profile['server'] else self._enroll(server)
                started.append(server)
            return {'started':started}
        if action=='receiver-stop':
            for server in self._attached_services():self._runtime(server).stop_and_wait()
            return {'stopped':True}
        if action=='approve-execution': return self._runtime(self.job_servers.get(data['job_id']))._control('action',dict(action='approve-execution',**data))
        if action=='settings-get':
            from team_service import TeamService
            return {'server':self.profile['server'],'services':[{'server':server,'primary':server==self.profile['server'],'attached':server in self._attached_services()} for server in self._services()],'background':self.profile.get('background',True),'autostart':False,'data_root':str(self.root),'team_service':TeamService(self.root,self.profile['server']).status()}
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
            if 'server' in data and data['server'].rstrip('/')!=self.profile['server']: raise ValueError('首个服务地址不可在此修改。加入其他服务请使用项目邀请。')
            if 'background' in data:self.profile['background']=bool(data['background']);write_json(self.root/'profiles'/(self.profile['id']+'.json'),self.profile)
            return {'saved':True}
        if action=='import-legacy': raise ValueError('旧版账户与会话不会自动合并。请继续使用旧版入口；迁移工具将在核对项目映射后提供。')
        raise ValueError('未知应用操作')
    def close(self):
        # All service operations happen before the user-approved window close.
        # The native closed event must never block on network/process cleanup.
        self.identity=None;self.pending=None
