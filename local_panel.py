"""Machine owner's loopback console; never exposed by the shared broker."""
import argparse
import html
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from common import read_json, write_json, validate_project
from pull_worker import Connector


def handler_for(connector, port):
    csrf = secrets.token_urlsafe(32)
    expected_host = f'127.0.0.1:{port}'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, value, status=200, content_type='application/json'):
            body = value.encode() if isinstance(value,str) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status); self.send_header('Content-Type',content_type+'; charset=utf-8')
            self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def valid_host(self):
            return self.headers.get('Host') == expected_host
        def do_GET(self):
            if not self.valid_host(): return self.respond({'error':'Loopback Host required'},403)
            if self.path == '/api/state':
                projects = [read_json(p) for p in (connector.root/'projects').glob('*/project.json')]
                pending = [{'job_id':r['job'].get('job_id',r['job'].get('id')), 'project':r['job']['backend_project'], 'template':r['job']['request']['template']} for p in (connector.root/'pull-receipts').glob('*.json') if (r:=read_json(p))['phase']=='waiting_local_approval']
                unknown=[{'job_id':r['job'].get('job_id',r['job'].get('id')),'project':r['job']['backend_project'],'error':r.get('result',{}).get('error','')} for p in (connector.root/'pull-receipts').glob('*.json') if (r:=read_json(p)).get('result',{}).get('status')=='unknown']
                templates = {p['name']:read_json(connector.root/'projects'/p['name']/'execution_templates.json',{}) for p in projects}
                return self.respond({'node':connector.config.get('node'),'bindings':connector.bindings(),'projects':projects,'shared_projects':read_json(connector.root/'broker-projects.json',[]),'pending':pending,'unknown':unknown,'templates':templates,'status':read_json(connector.root/'connector-status.json',{})})
            if self.path != '/': return self.respond({'error':'Not found'},404)
            return self.respond(PAGE.replace('__CSRF__',csrf),content_type='text/html')
        def do_POST(self):
            if not self.valid_host() or self.headers.get('Origin') != 'http://'+expected_host or self.headers.get('X-CSRF') != csrf:
                return self.respond({'error':'Local Origin and CSRF required'},403)
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=65536: raise ValueError('Invalid request size')
                data=json.loads(self.rfile.read(size)); action=data.pop('action')
                if action=='binding': result=connector.save_binding(data)
                elif action=='approve': connector.approve(data['job_id'],data['approved']); result={'saved':True}
                elif action=='resolve':result=connector.resolve(data['job_id'],bool(data.get('confirm_no_job')))
                elif action=='register':
                    from common import identifier, lock
                    if not Path(data['path']).is_absolute():raise ValueError('Working directory must be absolute')
                    project=validate_project({'name':identifier(data['name']),'remote_path':str(Path(data['path']).resolve()),'model':data['model'],'effort':data['effort'],'timeout':int(data.get('timeout',120)),'local_control':True})
                    result=connector.executor({'action':'register','project':project,'memory':{'revision':0,'entries':[]}})
                elif action=='templates':
                    result=connector.executor({'action':'execution_templates','project':data['project'],'templates':data['templates']})
                else: raise ValueError('Unknown local action')
                return self.respond({'ok':True,'result':result})
            except Exception as exc: return self.respond({'ok':False,'error':str(exc)},400)
    return Handler


PAGE = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>我的机器 · Agent Harness</title>
<style>body{font:16px system-ui;max-width:920px;margin:35px auto;padding:0 20px;background:#f4f6fa;color:#172033}section{background:white;border-radius:12px;padding:22px;margin:18px 0}input,select,textarea,button{font:inherit;padding:8px;margin:5px}textarea{width:95%;height:100px}button{cursor:pointer}label{display:inline-block}pre{white-space:pre-wrap}#error{color:#ad2530}</style>
<h1>我的机器</h1><p>这里的设置只保存在这台电脑。共享面板不能更改模型、工作目录或批准运行命令。</p><p id="status"></p><p id="error"></p>
<section><h2>接入项目</h2><p>先在本机注册工作目录，再选择共享项目。默认仅允许讨论。实现任务需要明确启用，且工作目录必须是专属 Git worktree。</p>
<form id="binding"><label>共享项目 <select name="project_id" id="shared"></select></label><label>本机执行项目 <select name="backend_project" id="local"></select></label><br>
<label>名称 <input name="name" placeholder="显示名称"></label><label>模型 <input name="model" required placeholder="gpt-5.6-sol"></label><label>思考强度 <select name="effort"><option>low</option><option selected>medium</option><option>high</option><option>xhigh</option><option>max</option><option>ultra</option></select></label><label>超时秒 <input name="timeout" type="number" value="120" min="30" max="86400"></label><br>
<label><input type="checkbox" name="enabled">启用此 agent</label><label><input type="checkbox" name="discuss" checked>讨论</label><label><input type="checkbox" name="review">检查</label><label><input type="checkbox" name="implement">修改工作区</label><button>保存本机设置</button></form><div id="bindings"></div></section>
<section><h2>等待本机批准的命令</h2><p>请检查完整固定参数，再批准。这一步只在本机完成；共享面板的批准不会替代它。</p><div id="pending"></div></section><section><h2>需要核对的未知任务</h2><p>核对只查询原任务，不会再次启动。若找不到记录，请先检查本机进程，再明确确认没有作业。</p><div id="unknown"></div></section>
<section><details><summary>注册工作目录 / 固定命令模板</summary><form id="register"><input name="name" required placeholder="本机项目名"><input name="path" required placeholder="绝对工作目录"><input name="model" required placeholder="模型"><select name="effort"><option>medium</option><option>high</option><option>low</option></select><button>注册工作目录</button></form><form id="templates"><select name="project" id="templateProject"></select><textarea name="templates" placeholder='{"check":{"argv":["D:/miniconda3/python.exe","check.py"]}}' required></textarea><button>保存固定模板</button></form></details></section>
<script>const csrf='__CSRF__';let state;const el=id=>document.getElementById(id);function text(tag,value){const e=document.createElement(tag);e.textContent=value;return e}async function post(data){const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF':csrf},body:JSON.stringify(data)});const j=await r.json();if(!j.ok)throw Error(j.error);await load()}function options(id,list,key,label){const s=el(id),old=s.value;s.replaceChildren();for(const p of list){const o=document.createElement('option');o.value=p[key];o.textContent=p[label]||p[key];s.append(o)}if([...s.options].some(o=>o.value===old))s.value=old}function edit(b){const f=el('binding');for(const k of ['project_id','backend_project','name','model','effort','timeout'])f.elements[k].value=b[k]||'';f.enabled.checked=b.enabled;for(const s of ['discuss','review','implement'])f.elements[s].checked=b.stages.includes(s)}async function load(){state=await(await fetch('/api/state')).json();el('status').textContent='机器：'+(state.node||'未配对')+' · '+(state.status.online?'连接正常':'等待连接');options('shared',state.shared_projects,'id','name');options('local',state.projects,'name','name');options('templateProject',state.projects,'name','name');el('bindings').replaceChildren();for(const b of state.bindings){const p=text('p',b.name+' · '+b.model+' / '+b.effort+' · '+(b.enabled?'启用':'停用')+' · '+b.stages.join(', '));const a=text('button','编辑');a.onclick=()=>edit(b);p.append(a);el('bindings').append(p)}el('pending').replaceChildren();for(const p of state.pending){const d=document.createElement('div');d.append(text('p',p.project+' / '+p.template),text('pre',JSON.stringify(state.templates[p.project]?.[p.template]||{},null,2)));for(const [label,approved]of [['批准',true],['拒绝',false]]){const b=text('button',label);b.onclick=()=>post({action:'approve',job_id:p.job_id,approved}).catch(showError);d.append(b)}el('pending').append(d)}el('unknown').replaceChildren();for(const u of state.unknown){const d=document.createElement('div');d.append(text('p',u.project+' · '+u.error));const checkbox=document.createElement('input');checkbox.type='checkbox';const label=text('label','我已检查并确认没有作业');label.prepend(checkbox);d.append(label);const b=text('button','核对原任务');b.onclick=()=>post({action:'resolve',job_id:u.job_id,confirm_no_job:checkbox.checked}).catch(showError);d.append(b);el('unknown').append(d)}}function showError(e){el('error').textContent=e.message}for(const id of ['binding','register','templates'])el(id).onsubmit=async e=>{e.preventDefault();const f=e.target,d=Object.fromEntries(new FormData(f));d.action=id;if(id==='binding'){d.timeout=Number(d.timeout);d.enabled=f.enabled.checked;d.stages=['discuss','review','implement'].filter(s=>f.elements[s].checked);for(const s of ['discuss','review','implement'])delete d[s]}if(id==='templates')try{d.templates=JSON.parse(d.templates)}catch(e){showError(e);return}try{el('error').textContent='';await post(d)}catch(e){showError(e)}};load().catch(showError);setInterval(()=>load().catch(showError),5000);</script></html>'''


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True);parser.add_argument('--port',type=int,default=8787)
    args=parser.parse_args(); connector=Connector(args.config)
    print(f'Local owner console: http://127.0.0.1:{args.port}')
    ThreadingHTTPServer(('127.0.0.1',args.port),handler_for(connector,args.port)).serve_forever()


if __name__=='__main__': main()
