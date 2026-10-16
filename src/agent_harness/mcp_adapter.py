"""Codex-facing MCP adapter for the local Agent Harness account.

The adapter is a stdio process.  It opens no listening port and holds only a
server-enforced, capability-scoped delegate credential created by the desktop
control plane.  Project services remain authoritative for membership,
conversation visibility, employee ownership, and task state.
"""
import argparse
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from account_identity import sign_envelope
from common import read_json
from pull_worker import NoRedirect, validate_server
from mcp_server import run_stdio


class HarnessMcpBackend:
    def __init__(self, grant_path):
        self.grant_path=Path(grant_path).expanduser().resolve()
        self.grant=self._load_grant()
        self.project_servers={}
        self.last_dashboard=0.0
        self.dashboard_cache=None

    def _load_grant(self):
        value=read_json(self.grant_path)
        required={'version','identity','profile_id','username','servers','scopes'}
        if not isinstance(value,dict) or value.get('version')!=1 or not required.issubset(value):
            raise PermissionError('Agent Harness Codex integration is not enabled for this account')
        identity=value.get('identity')
        if not isinstance(identity,dict) or not all(isinstance(identity.get(key),str) for key in ('private_key','public_key','account_id')):
            raise PermissionError('Agent Harness Codex delegate credential is invalid')
        scopes=value.get('scopes')
        if not isinstance(scopes,list) or not {'collaboration.read','collaboration.message','collaboration.task'}.issubset(scopes):
            raise PermissionError('Agent Harness Codex delegate scopes are incomplete')
        servers=value.get('servers')
        if not isinstance(servers,list) or not servers:raise PermissionError('Agent Harness has no collaboration service')
        for server in servers:validate_server(server)
        return value

    def _post(self,server,data,timeout=12):
        validate_server(server)
        request=urllib.request.Request(server.rstrip('/')+'/api/account',
            data=json.dumps(data,ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type':'application/json'})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=timeout) as response:
                result=json.load(response)
        except urllib.error.HTTPError as exc:
            try:message=json.load(exc).get('error','协作服务拒绝请求')
            except Exception:message='协作服务拒绝请求'
            raise PermissionError(message) from None
        except urllib.error.URLError:
            raise RuntimeError('无法连接协作服务 '+server+'；请确认托管电脑和 Tailscale 在线') from None
        if not isinstance(result,dict):raise RuntimeError('协作服务返回无效数据')
        if result.get('error'):raise RuntimeError(result['error'])
        return result.get('result',result)

    def _rpc(self,server,operation,payload=None,timeout=12):
        identity=self.grant['identity']
        challenge=self._post(server,{'action':'challenge','public_key':identity['public_key']},timeout)
        features=set(challenge.get('features',[]))
        if 'codex_mcp_v1' not in features:
            raise RuntimeError('协作服务版本过旧，尚未启用 Codex MCP 权限边界')
        envelope=sign_envelope(identity,{'domain':'harness-account-request-v1','nonce':challenge['nonce'],
                                         'operation':operation,'payload':payload or {}})
        return self._post(server,{'action':'rpc','envelope':envelope},timeout)

    def _services(self):
        services=list(dict.fromkeys(self.grant['servers']))
        primary=services[0]
        try:
            linked=self._rpc(primary,'service-links',timeout=5).get('servers',[])
        except (OSError,RuntimeError,PermissionError,ValueError):linked=[]
        for server in linked:
            try:validate_server(server)
            except (TypeError,ValueError):continue
            if server not in services:services.append(server)
        return services

    def _dashboards(self,force=False):
        if not force and self.dashboard_cache is not None and time.monotonic()-self.last_dashboard<3:
            return self.dashboard_cache
        available=[];unavailable=[];projects={};service_ids=set()
        for server in self._services():
            try:data=self._rpc(server,'dashboard',timeout=6)
            except (OSError,RuntimeError,PermissionError,ValueError) as exc:
                unavailable.append({'server':server,'error':str(exc)});continue
            service_id=data.get('service_id')
            if service_id and service_id in service_ids:continue
            if service_id:service_ids.add(service_id)
            available.append({'server':server,'data':data})
            for project in data.get('projects',[]):
                projects[project['id']]=server
        self.project_servers=projects
        self.dashboard_cache={'available':available,'unavailable':unavailable}
        self.last_dashboard=time.monotonic()
        return self.dashboard_cache

    def _project_server(self,project):
        self._dashboards()
        server=self.project_servers.get(project)
        if not server:
            self._dashboards(force=True);server=self.project_servers.get(project)
        if not server:raise ValueError('当前账户看不到这个项目，或其协作服务暂时离线')
        return server

    def tool_specs(self):
        obj={'type':'object','additionalProperties':False}
        return [
            {'name':'harness_status','description':'检查 Agent Harness 账户及各协作服务连接状态。',
             'inputSchema':dict(obj,properties={})},
            {'name':'harness_projects_list','description':'列出当前 Harness 账户参加的项目及其托管服务状态。',
             'inputSchema':dict(obj,properties={})},
            {'name':'harness_project_read','description':'读取一个项目中当前账户有权查看的成员、会话、消息、任务与共享记忆。可按会话和消息游标缩小结果。',
             'inputSchema':dict(obj,properties={
                 'project':{'type':'string'},'conversation_id':{'type':'string'},
                 'after_message_id':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':200}},required=['project'])},
            {'name':'harness_message_send','description':'以“当前账户 · Codex”来源向项目群聊、自己的 Agent 私聊或另一位人类私聊发送消息。群聊可点名一个 Agent。',
             'inputSchema':dict(obj,properties={
                 'project':{'type':'string'},'body':{'type':'string','minLength':1,'maxLength':16000},
                 'conversation_id':{'type':'string'},'recipient_id':{'type':'string'},'mention_agent_id':{'type':'string'},
                 'client_message_id':{'type':'string'}},required=['project','body'])},
            {'name':'harness_task_assign','description':'给当前账户拥有且已派入项目的 Agent 创建异步任务。立即返回任务编号；不要在本次调用中等待模型完成。',
             'inputSchema':dict(obj,properties={
                 'project':{'type':'string'},'agent':{'type':'string'},'title':{'type':'string'},
                 'instructions':{'type':'string'},'stage':{'type':'string','enum':['discuss','review','implement']},
                 'allowed_files':{'type':'string'},'acceptance':{'type':'string'},'validation':{'type':'string'}},
                 required=['project','agent','title','instructions','acceptance','validation'])},
            {'name':'harness_task_get','description':'读取异步任务的当前状态、结果或错误。',
             'inputSchema':dict(obj,properties={'project':{'type':'string'},'task':{'type':'string'}},required=['project','task'])},
        ]

    def call(self,name,arguments):
        if not isinstance(arguments,dict):raise ValueError('Tool arguments must be an object')
        if name=='harness_status':
            data=self._dashboards(force=True)
            return {'account':self.grant['username'],'profile_id':self.grant['profile_id'],
                    'connected_services':[item['server'] for item in data['available']],
                    'unavailable_services':data['unavailable']}
        if name=='harness_projects_list':
            data=self._dashboards(force=True);projects=[]
            for item in data['available']:
                for project in item['data'].get('projects',[]):
                    projects.append({key:project.get(key) for key in ('id','name','owner_id','admin_agent_id','paused','last_entered')}|{'server':item['server']})
            return {'projects':projects,'unavailable_services':data['unavailable']}
        if name=='harness_project_read':
            project=arguments['project'];result=self._rpc(self._project_server(project),'project',{'project':project})
            messages=result.get('messages',[]);conversation=arguments.get('conversation_id')
            if conversation:messages=[message for message in messages if message.get('conversation_id')==conversation]
            after=arguments.get('after_message_id',0)
            if not isinstance(after,int) or isinstance(after,bool) or after<0:raise ValueError('after_message_id 必须是非负整数')
            messages=[message for message in messages if int(message.get('id',0))>after]
            limit=arguments.get('limit',100)
            if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=200:raise ValueError('limit 必须是 1 到 200')
            result['messages']=messages[-limit:]
            result['server']=self.project_servers[project]
            return result
        if name=='harness_message_send':
            project=arguments['project'];payload={key:arguments[key] for key in ('project','body','conversation_id','recipient_id','mention_agent_id') if key in arguments}
            message_id=arguments.get('client_message_id') or str(uuid.uuid4())
            try:message_id=str(uuid.UUID(message_id))
            except (ValueError,TypeError,AttributeError):raise ValueError('client_message_id 必须是 UUID') from None
            payload['client_message_id']=message_id
            result=self._rpc(self._project_server(project),'message',payload)
            return {'project':project,'client_message_id':message_id,**result}
        if name=='harness_task_assign':
            project=arguments['project'];payload=dict(arguments)
            payload.setdefault('stage','discuss');payload.setdefault('allowed_files','只讨论，不修改文件')
            result=self._rpc(self._project_server(project),'task',payload)
            return {'project':project,**result}
        if name=='harness_task_get':
            project=arguments['project']
            return self._rpc(self._project_server(project),'task-status',{'task':arguments['task']})
        raise ValueError('Unknown Harness tool')


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--grant',required=True);args=parser.parse_args(argv)
    run_stdio(HarnessMcpBackend(args.grant))


if __name__=='__main__':main()
