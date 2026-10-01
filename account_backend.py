"""Proof-of-possession account API; the service never receives local passwords."""
import hashlib
import json
import secrets
import time
from account_identity import account_id, decode, verify_envelope, canonical
from collaboration_store import digest
from owner_backend import OwnerBackend
from owner_worker import execution_project

def device_node(public): return 'device-'+hashlib.sha256(public.encode()).hexdigest()[:24]

class AccountBackend:
    def __init__(self,store,backend,action_callback=None):
        self.store,self.backend,self.action_callback=store,backend,action_callback;self.owners=OwnerBackend(store)
        with store.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS account_devices(public_key TEXT PRIMARY KEY,account_id TEXT NOT NULL,name TEXT NOT NULL,chain TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS account_challenges(nonce TEXT PRIMARY KEY,public_key TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS account_requests(id TEXT PRIMARY KEY,code_hash TEXT UNIQUE NOT NULL,public_key TEXT NOT NULL,name TEXT NOT NULL,expires REAL NOT NULL,account_id TEXT,chain TEXT,claimed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS account_project_visits(account_id TEXT NOT NULL,project_id TEXT NOT NULL,visited REAL NOT NULL,PRIMARY KEY(account_id,project_id));
            CREATE TABLE IF NOT EXISTS account_services(account_id TEXT NOT NULL,server TEXT NOT NULL,joined_at REAL NOT NULL,PRIMARY KEY(account_id,server));
            ''')
    def api(self,data):
        if not isinstance(data,dict):raise ValueError('Expected account request')
        action=data.get('action')
        if action=='challenge':
            public=data['public_key'];decode(public,32);nonce=secrets.token_urlsafe(32)
            with self.store.connect() as db:
                db.execute('DELETE FROM account_challenges WHERE expires<?',(time.time(),));db.execute('INSERT INTO account_challenges VALUES(?,?,?)',(nonce,public,time.time()+60))
            return {'nonce':nonce,'expires_in':60,'features':['mention_agent_id','anchored_reply_draft']}
        if action=='request-device':
            public=data['public_key'];decode(public,32);name=self._name(data.get('name','新电脑'));code=secrets.token_urlsafe(24);rid='request-'+secrets.token_hex(16)
            with self.store.connect() as db:
                db.execute('INSERT INTO account_requests(id,code_hash,public_key,name,expires) VALUES(?,?,?,?,?)',(rid,digest(code),public,name,time.time()+600))
            return {'request_id':rid,'code':code,'expires_in':600}
        if action=='claim-device':
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT * FROM account_requests WHERE id=? AND code_hash=? AND expires>? AND claimed=0',(data['request_id'],digest(data['code']),time.time())).fetchone()
                if not row:raise PermissionError('Device request unavailable')
                if not row['account_id']:return {'state':'pending','status':'pending'}
                device=db.execute('SELECT revoked FROM account_devices WHERE public_key=?',(row['public_key'],)).fetchone()
                if not device or device['revoked']:raise PermissionError('Device approval revoked')
                db.execute('UPDATE account_requests SET claimed=1 WHERE id=?',(row['id'],))
                return {'state':'approved','status':'approved','account_id':row['account_id'],'username':self._username(db,row['account_id']),'node':device_node(row['public_key']),'public_key':row['public_key'],'chain':json.loads(row['chain'])}
        if action not in {'register','rpc','accept-project-invite','activate-device'}:raise ValueError('Unknown account operation')
        verified=verify_envelope(data['envelope']);body=verified['body'];public=verified['public_key'];user=verified['account_id']
        if body.get('domain')!='harness-account-request-v1' or not isinstance(body.get('payload'),dict):raise PermissionError('Wrong request domain')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');challenge=db.execute('SELECT * FROM account_challenges WHERE nonce=? AND public_key=? AND expires>?',(body.get('nonce'),public,time.time())).fetchone()
            if not challenge:raise PermissionError('Challenge expired or already used')
            db.execute('DELETE FROM account_challenges WHERE nonce=?',(body['nonce'],))
        # Consume even rejected authenticated operations to prevent replay after state changes.
        if action=='register':
            if body.get('operation')!='register' or verified['chain_keys']!=[public]:raise PermissionError('Registration requires a root identity')
            name=self._name(body['payload'].get('name'));device_name=self._name(body['payload'].get('device_name','我的电脑'))
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM users WHERE id=?',(user,)).fetchone():raise ValueError('Account already registered')
                db.execute('INSERT INTO users VALUES(?,?,?,?)',(user,name+' · '+user[-8:],secrets.token_hex(16),secrets.token_hex(64)))
                db.execute('INSERT INTO account_devices VALUES(?,?,?,?,0)',(public,user,device_name,'[]'))
                self._node(db,public,user)
            return {'account_id':user,'name':name,'username':name,'node':device_node(public),'public_key':public}
        if action=='activate-device':
            if body.get('operation')!='activate-device':raise PermissionError('Wrong device proof')
            device_name=self._name(body['payload'].get('device_name','我的电脑'))
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if not db.execute('SELECT 1 FROM users WHERE id=?',(user,)).fetchone():raise PermissionError('此身份尚未加入该服务的项目')
                self._register_chain(db,verified,data['envelope'].get('chain',[]),device_name)
            return {'account_id':user,'node':device_node(public)}
        if action=='accept-project-invite':
            if body.get('operation')!='accept-project-invite':raise PermissionError('Wrong invite proof')
            payload=body['payload'];name=self._name(payload.get('name'));device_name=self._name(payload.get('device_name','我的电脑'))
            token=payload.get('invite')
            if not isinstance(token,str) or len(token)>256:raise PermissionError('邀请无效或已使用')
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                invitation=db.execute('SELECT * FROM invitations WHERE hash=? AND used=0 AND expires>?',(digest(token),time.time())).fetchone()
                if not invitation:raise PermissionError('邀请无效或已使用')
                if not db.execute('SELECT 1 FROM users WHERE id=?',(user,)).fetchone():
                    db.execute('INSERT INTO users VALUES(?,?,?,?)',(user,name+' · '+user[-8:],secrets.token_hex(16),secrets.token_hex(64)))
                self._register_chain(db,verified,data['envelope'].get('chain',[]),device_name)
                db.execute('INSERT OR IGNORE INTO project_users VALUES(?,?)',(invitation['project_id'],user))
                db.execute('UPDATE invitations SET used=1 WHERE hash=?',(digest(token),))
                self.store.log(db,user,'accept_project_invite',{'project':invitation['project_id']})
                return {'project':invitation['project_id'],'account_id':user,'node':device_node(public)}
        with self.store.connect() as db:
            for key in verified['chain_keys']:
                device=db.execute('SELECT * FROM account_devices WHERE public_key=? AND account_id=?',(key,user)).fetchone()
                if not device or device['revoked']:raise PermissionError('Device is not authorized')
        return self._operation(user,public,body['operation'],body['payload'])
    def _name(self,name):
        if not isinstance(name,str) or not 1<=len(name.strip())<=120:raise ValueError('Name must contain 1–120 characters')
        return name.strip()
    def _register_chain(self,db,verified,chain,device_name):
        user,public=verified['account_id'],verified['public_key']
        keys=[verified['root_public_key']]+[cert['body']['child'] for cert in chain]
        for index,key in enumerate(keys):
            row=db.execute('SELECT account_id,revoked FROM account_devices WHERE public_key=?',(key,)).fetchone()
            if row and (row['account_id']!=user or row['revoked']):raise PermissionError('设备授权已撤销或属于其他账户')
            if not row:db.execute('INSERT INTO account_devices VALUES(?,?,?,?,0)',(key,user,device_name if key==public else '关联设备',json.dumps(chain[:index])))
            self._node(db,key,user)
    def _operation(self,user,public,op,p):
        if op=='service-links':
            with self.store.connect() as db:
                return {'servers':[row['server'] for row in db.execute('SELECT server FROM account_services WHERE account_id=? ORDER BY joined_at',(user,))]}
        if op=='link-service':
            from pull_worker import validate_server
            server=p['server'].rstrip('/');validate_server(server)
            with self.store.connect() as db:
                db.execute('INSERT OR IGNORE INTO account_services VALUES(?,?,?)',(user,server,time.time()))
            return {'server':server}
        if op=='dashboard':
            result=self.store.snapshot(user);result.update(self.owners.snapshot(user))
            with self.store.connect() as db:
                result['user']={'id':user,'username':self._username(db,user)}
                visits={r['project_id']:r['visited'] for r in db.execute('SELECT * FROM account_project_visits WHERE account_id=?',(user,))}
                for project in result['projects']:project['last_entered']=visits.get(project['id'])
                devices=[]
                for row in db.execute('SELECT * FROM account_devices WHERE account_id=? AND revoked=0',(user,)):
                    node=self._node(db,row['public_key'],user)
                    seen=db.execute('SELECT last_seen FROM pull_devices WHERE node=?',(node,)).fetchone()
                    last_seen=seen['last_seen'] if seen else None
                    devices.append({'id':node,'node':node,'name':row['name'],'public_key':row['public_key'],'is_root':account_id(row['public_key'])==user,'online':bool(last_seen and last_seen>time.time()-45),'last_seen':last_seen})
                result['devices']=devices;device_map={d['node']:d for d in devices}
                for employee in result['employees']:
                    employee.update(employee.pop('settings'));employee['path']=employee.get('remote_path','')
                    pending=[]
                    for command in db.execute("SELECT id,envelope,state FROM owner_commands WHERE owner_id=? AND node=? AND state IN ('queued','claimed')",(user,employee['node'])):
                        if json.loads(command['envelope'])['body']['data'].get('id')==employee['id']:pending.append(command['id'])
                    for row in db.execute('SELECT envelope,state,result FROM owner_commands WHERE owner_id=? AND node=? ORDER BY created_at DESC',(user,employee['node'])):
                        if json.loads(row['envelope'])['body']['data'].get('id')==employee['id']:
                            if row['state']=='failed':employee['last_error']=json.loads(row['result'] or '{}').get('error','操作失败')
                            break
                    employee['pending']=pending;employee['online']=device_map.get(employee['node'],{}).get('online',False)
                    if pending:employee['status']='pending'
                    elif not employee['online']:employee['status']='offline'
            return result
        if op=='project':
            project=p.get('project',p.get('id'));result=self.store.project_view(user,project)
            with self.store.connect() as db:
                if p.get('visit') is True:db.execute('INSERT INTO account_project_visits VALUES(?,?,?) ON CONFLICT(account_id,project_id) DO UPDATE SET visited=excluded.visited',(user,project,time.time()))
                employees=db.execute('SELECT id,node FROM employees WHERE owner_id IN (SELECT user_id FROM project_users WHERE project_id=?)',(project,)).fetchall()
                dispatch_state={}
                for command in db.execute("SELECT envelope FROM owner_commands WHERE state='applied' AND owner_id IN (SELECT user_id FROM project_users WHERE project_id=?) ORDER BY created_at,rowid",(project,)):
                    body=json.loads(command['envelope'])['body'];data=body['data']
                    if body['action'] in {'dispatch','withdraw'} and data.get('project_id')==project:dispatch_state[data['id']]=body['action']
                for member in result.get('members',[]):
                    member['employee_id']=next((e['id'] for e in employees if e['node']==member['node'] and execution_project(e['id'],project)==member['backend_project']),None)
                    seen=db.execute('SELECT last_seen FROM pull_devices WHERE node=?',(member['node'],)).fetchone()
                    member['online']=bool(seen and seen['last_seen']>time.time()-45)
                result['members']=[m for m in result.get('members',[]) if dispatch_state.get(m.get('employee_id'))!='withdraw']
            return result
        if op=='create-project':return self.store.project_create(user,p['name'])
        if op in {'join','join-project'}:return self.store.join(user,p['invite'])
        if op=='invite':return self.store.invite(user,p['project'])
        if op=='message':return self.store.message(user,p['project'],p['body'],p.get('recipient_id'),p.get('mention_agent_id'))
        if op=='set-admin':
            if p.get('agent'):return self.store.set_admin(user,p['project'],p['agent']) or {'updated':True}
            with self.store.connect() as db:
                project=self.store.require_member(db,p['project'],user)
                if project['owner_id']!=user:raise PermissionError('只有群主可以指定管理员')
                db.execute('UPDATE projects SET admin_agent_id=NULL WHERE id=?',(p['project'],))
            return {'updated':True}
        if op=='owner-command':
            verified=verify_envelope(p['envelope'],user)
            with self.store.connect() as db:
                for key in verified['chain_keys']:
                    row=db.execute('SELECT revoked FROM account_devices WHERE public_key=? AND account_id=?',(key,user)).fetchone()
                    if not row or row['revoked']:raise PermissionError('Owner command signer was revoked')
            return self.owners.enqueue(user,p['envelope'])
        if op=='command-status':return self.owners.command(user,p['id'])
        if op=='pair-local':
            node=device_node(public)
            if p.get('node',node)!=node:raise PermissionError('Cannot re-pair another device')
            return self.backend.create_pair(user,node)
        if op in {'device-request','inspect-device'}:
            with self.store.connect() as db:
                row=db.execute('SELECT id,public_key,name,account_id,expires FROM account_requests WHERE code_hash=? AND expires>? AND claimed=0',(digest(p['code']),time.time())).fetchone()
                if not row or row['account_id']:raise PermissionError('Device request unavailable')
                return dict(row,request_id=row['id'])
        if op=='approve-device':
            code=p.get('code');rid=p.get('request_id')
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT * FROM account_requests WHERE '+('id=?' if rid else 'code_hash=?')+' AND expires>? AND claimed=0',(rid or digest(code or ''),time.time())).fetchone()
                if not row or row['account_id']:raise PermissionError('Device request unavailable')
                chain=p['chain']
                # Child proof is not available to approving device. Verify certificates via
                # their parent signatures separately, using certificate helper below.
                self._verify_chain(chain,user,row['public_key'],public)
                db.execute('INSERT INTO account_devices VALUES(?,?,?,?,0)',(row['public_key'],user,row['name'],json.dumps(chain)))
                self._node(db,row['public_key'],user)
                db.execute('UPDATE account_requests SET account_id=?,chain=? WHERE id=?',(user,json.dumps(chain),row['id']))
            return {'approved':True,'request_id':row['id']}
        if op=='revoke-device':
            with self.store.connect() as db:
                target=p.get('public_key')
                if not target:
                    target=next((r['public_key'] for r in db.execute('SELECT public_key FROM account_devices WHERE account_id=?',(user,)) if device_node(r['public_key'])==p.get('id')),None)
                row=db.execute('SELECT * FROM account_devices WHERE public_key=? AND account_id=?',(target,user)).fetchone()
                if not row:raise PermissionError('Unknown device')
                if target==public:raise ValueError('Cannot revoke the device making this request')
                if account_id(target)==user:raise ValueError('Cannot revoke the root identity device; account recovery is not yet supported')
                revoked_keys={target}
                for candidate in db.execute('SELECT public_key,chain FROM account_devices WHERE account_id=?',(user,)):
                    ancestors={cert['body']['parent'] for cert in json.loads(candidate['chain'])}
                    if target in ancestors:revoked_keys.add(candidate['public_key'])
                revoked_nodes={device_node(key) for key in revoked_keys}
                for key in revoked_keys:
                    db.execute('UPDATE account_devices SET revoked=1 WHERE public_key=?',(key,))
                    node=device_node(key)
                    db.execute('DELETE FROM pull_devices WHERE node=?',(node,))
                    db.execute('UPDATE agents SET enabled=0 WHERE node=?',(node,))
                for command in db.execute("SELECT id,envelope FROM owner_commands WHERE owner_id=? AND state IN ('queued','claimed')",(user,)).fetchall():
                    envelope=json.loads(command['envelope']);keys=[envelope.get('public_key')]+[cert['body']['parent'] for cert in envelope.get('chain',[])]
                    if command['envelope'] and (revoked_keys.intersection(keys) or envelope['body']['target'] in revoked_nodes):
                        db.execute("UPDATE owner_commands SET state='failed',result=? WHERE id=?",(canonical({'error':'Device revoked'}).decode(),command['id']))
            return {'revoked':True}
        raise ValueError('Unsupported signed account operation')
    def _username(self,db,user):
        name=db.execute('SELECT username FROM users WHERE id=?',(user,)).fetchone()['username']
        suffix=' · '+user[-8:]
        return name[:-len(suffix)] if name.endswith(suffix) else name
    def _node(self,db,public,user):
        node=device_node(public);existing=db.execute('SELECT owner_id FROM nodes WHERE name=?',(node,)).fetchone()
        if existing and existing['owner_id']!=user:raise PermissionError('Device node belongs to another account')
        db.execute('INSERT OR IGNORE INTO nodes VALUES(?,?)',(node,user));return node
    def _verify_chain(self,chain,user,child,approver):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        if not isinstance(chain,list) or not 1<=len(chain)<=8:raise PermissionError('Invalid approval chain')
        parent=chain[0]['body']['parent'];seen={parent}
        if account_id(parent)!=user:raise PermissionError('Wrong root')
        for cert in chain:
            b=cert['body']
            if set(b)!={'domain','account_id','parent','child'} or b['domain']!='harness-device-certificate-v1' or b['account_id']!=user or b['parent']!=parent or b['child'] in seen:raise PermissionError('Invalid certificate')
            Ed25519PublicKey.from_public_bytes(decode(parent,32)).verify(decode(cert['signature'],64),canonical(b));parent=b['child'];seen.add(parent)
        if parent!=child or chain[-1]['body']['parent']!=approver:raise PermissionError('Approval must be signed by the requesting device')
