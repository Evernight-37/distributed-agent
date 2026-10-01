"""Project groups, identities, ownership and recipient-filtered durable messages."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def uid(prefix):
    return prefix + '-' + uuid.uuid4().hex


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('密码长度必须为 12–256 字符')
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'collaboration.sqlite3'
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id TEXT,csrf TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,owner_id TEXT NOT NULL,admin_agent_id TEXT,paused INTEGER NOT NULL DEFAULT 0,budget_remaining INTEGER NOT NULL DEFAULT 20);
            CREATE TABLE IF NOT EXISTS project_users(project_id TEXT,user_id TEXT,PRIMARY KEY(project_id,user_id));
            CREATE TABLE IF NOT EXISTS nodes(name TEXT PRIMARY KEY,owner_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,name TEXT NOT NULL,owner_id TEXT NOT NULL,node TEXT NOT NULL,backend_project TEXT NOT NULL,model TEXT NOT NULL,effort TEXT NOT NULL,timeout INTEGER NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,last_message INTEGER NOT NULL DEFAULT 0,UNIQUE(node,backend_project));
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT NOT NULL,sender_id TEXT NOT NULL,sender_type TEXT NOT NULL,recipient_id TEXT,body TEXT NOT NULL,created_at REAL NOT NULL,causal_task TEXT);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,agent_id TEXT NOT NULL,title TEXT NOT NULL,contract TEXT NOT NULL,state TEXT NOT NULL,job TEXT,report TEXT,created_at REAL NOT NULL,cutoff INTEGER NOT NULL DEFAULT 0,error TEXT,processed INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_task ON tasks(agent_id) WHERE state IN ('queued','starting','running','unknown');
            CREATE TABLE IF NOT EXISTS executions(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,agent_id TEXT NOT NULL,template TEXT NOT NULL,state TEXT NOT NULL,result TEXT,created_at REAL NOT NULL,notified INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS invitations(hash TEXT PRIMARY KEY,project_id TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT,action TEXT,details TEXT,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_cursors(agent_id TEXT,channel TEXT,last_message INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(agent_id,channel));
            ''')
            if 'configuring' not in {r['name'] for r in db.execute('PRAGMA table_info(agents)')}:
                db.execute('ALTER TABLE agents ADD COLUMN configuring INTEGER NOT NULL DEFAULT 0')
            if 'reply_to' not in {r['name'] for r in db.execute('PRAGMA table_info(executions)')}:
                db.execute('ALTER TABLE executions ADD COLUMN reply_to TEXT')
            for table in ('tasks','executions'):
                if 'cancel_requested' not in {r['name'] for r in db.execute('PRAGMA table_info('+table+')')}:
                    db.execute('ALTER TABLE '+table+' ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def log(self, db, actor, action, details):
        db.execute('INSERT INTO audit(actor,action,details,created_at) VALUES(?,?,?,?)', (actor, action, json.dumps(details, ensure_ascii=False), time.time()))

    def bootstrap_required(self):
        with self.connect() as db:
            return not db.execute('SELECT 1 FROM users LIMIT 1').fetchone()

    def session(self, token=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=? AND expires>?', (digest(token or ''), time.time())).fetchone()
            if row:
                user = db.execute('SELECT id,username FROM users WHERE id=?', (row['user_id'],)).fetchone()
                return token, row['csrf'], dict(user) if user else None
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (digest(token), None, csrf, time.time()+86400))
            return token, csrf, None

    def authenticate(self, username, password, token):
        with self.connect() as db:
            user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            salt = user['salt'] if user else '00'*16
            calculated = password_hash(password, salt)
            if not user or not hmac.compare_digest(calculated, user['password_hash']):
                raise PermissionError('用户名或密码错误')
            db.execute('DELETE FROM sessions WHERE id=?', (digest(token),))
            fresh, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (digest(fresh), user['id'], csrf, time.time()+86400))
            return fresh, csrf, {'id':user['id'],'username':user['username']}

    def signup(self, username, password, bootstrap=False, invite=None):
        if not re.fullmatch(r'[A-Za-z0-9_\-]{1,40}', username or ''):
            raise ValueError('用户名使用 1–40 个字母、数字、下划线或连字符')
        salt = secrets.token_hex(16)
        hashed = password_hash(password, salt)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = None
            if bootstrap:
                if db.execute('SELECT 1 FROM users').fetchone():
                    raise PermissionError('初始账户已创建')
            else:
                invitation = db.execute('SELECT * FROM invitations WHERE hash=? AND used=0 AND expires>?', (digest(invite or ''),time.time())).fetchone()
                if not invitation:
                    raise PermissionError('需要有效的项目邀请')
                project = invitation['project_id']
            user_id = uid('user')
            db.execute('INSERT INTO users VALUES(?,?,?,?)',(user_id,username,salt,hashed))
            if project:
                db.execute('INSERT INTO project_users VALUES(?,?)',(project,user_id))
                db.execute('UPDATE invitations SET used=1 WHERE hash=?',(digest(invite),))
            self.log(db,user_id,'signup',{'bootstrap':bootstrap})
            return {'id':user_id,'username':username}

    def require_member(self, db, project, user):
        row = db.execute('SELECT * FROM projects WHERE id=?',(project,)).fetchone()
        if not row or not db.execute('SELECT 1 FROM project_users WHERE project_id=? AND user_id=?',(project,user)).fetchone():
            raise PermissionError('无权访问这个项目')
        return dict(row)

    def require_agent_owner(self, db, agent, user):
        row = db.execute('SELECT * FROM agents WHERE id=?',(agent,)).fetchone()
        if not row or row['owner_id'] != user:
            raise PermissionError('只有所有者可以控制这个 agent')
        self.require_member(db,row['project_id'],user)
        return dict(row)

    def project_create(self, user, name):
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError('项目名称不能为空，最多 120 字符')
        with self.connect() as db:
            project = uid('project')
            db.execute('INSERT INTO projects(id,name,owner_id) VALUES(?,?,?)',(project,name.strip(),user))
            db.execute('INSERT INTO project_users VALUES(?,?)',(project,user))
            self.log(db,user,'project_create',{'project':project})
            return {'id':project,'name':name.strip()}

    def invite(self, user, project):
        token = secrets.token_urlsafe(24)
        with self.connect() as db:
            row = self.require_member(db,project,user)
            if row['owner_id'] != user:
                raise PermissionError('只有群主可以邀请成员')
            db.execute('INSERT INTO invitations VALUES(?,?,?,0)',(digest(token),project,time.time()+86400))
            self.log(db,user,'invite',{'project':project})
        return {'invite':token,'expires_in':86400}

    def join(self, user, token):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            invitation = db.execute('SELECT * FROM invitations WHERE hash=? AND used=0 AND expires>?',(digest(token),time.time())).fetchone()
            if not invitation:
                raise PermissionError('邀请无效或已使用')
            db.execute('INSERT OR IGNORE INTO project_users VALUES(?,?)',(invitation['project_id'],user))
            db.execute('UPDATE invitations SET used=1 WHERE hash=?',(digest(token),))
            return {'project':invitation['project_id']}

    def bind_node(self, user, name):
        # Trusted local CLI only; deliberately absent from the HTTP action router.
        from common import identifier, read_json
        identifier(name)
        if not read_json(self.root/'nodes'/(name+'.json')):
            raise ValueError('请先通过 CLI 注册连接')
        with self.connect() as db:
            existing = db.execute('SELECT * FROM nodes WHERE name=?',(name,)).fetchone()
            if existing and existing['owner_id'] != user:
                raise PermissionError('这个节点已属于其他人')
            db.execute('INSERT OR IGNORE INTO nodes VALUES(?,?)',(name,user))
            self.log(db,user,'bind_node',{'name':name})

    def import_legacy(self, user):
        """First local owner adopts existing connections/sessions without model calls."""
        from common import read_json
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            first = db.execute('SELECT id FROM users ORDER BY rowid LIMIT 1').fetchone()
            if not first or first['id'] != user: raise PermissionError('仅初始本机账户可以迁移已有注册')
            for path in (self.root/'nodes').glob('*.json'):
                db.execute('INSERT OR IGNORE INTO nodes VALUES(?,?)', (path.stem,user))
            groups={}
            mapping=read_json(self.root/'group_import.json',{})
            for path in sorted((self.root/'projects').glob('*.json')):
                backend = read_json(path,{})
                node = backend.get('node')
                if not node or not db.execute('SELECT 1 FROM nodes WHERE name=? AND owner_id=?',(node,user)).fetchone(): continue
                if db.execute('SELECT 1 FROM agents WHERE node=? AND backend_project=?',(node,path.stem)).fetchone(): continue
                group_name=mapping.get(path.stem,path.stem)
                agent=uid('agent')
                if group_name not in groups:
                    project=uid('project')
                    groups[group_name]=project
                    db.execute('INSERT INTO projects(id,name,owner_id,admin_agent_id) VALUES(?,?,?,?)',(project,group_name,user,agent))
                    db.execute('INSERT INTO project_users VALUES(?,?)',(project,user))
                project=groups[group_name]
                db.execute('INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)',
                    (agent,project,node,user,node,path.stem,backend['model'],backend['effort'],backend['timeout']))
            self.log(db,user,'import_legacy',{})

    def add_agent(self, user, data, backend):
        from common import identifier
        identifier(data['backend_project']); identifier(data['node'])
        name = str(data['name']).strip()
        if not 1 <= len(name) <= 80: raise ValueError('agent 名称不能为空，最多 80 字符')
        with self.connect() as db:
            self.require_member(db,data['project'],user)
            node = db.execute('SELECT * FROM nodes WHERE name=?',(data['node'],)).fetchone()
            if not node or node['owner_id'] != user:
                raise PermissionError('只能接入自己的机器')
            if db.execute('SELECT 1 FROM agents WHERE node=? AND backend_project=?',(data['node'],data['backend_project'])).fetchone():
                raise ValueError('执行会话已接入，不能在项目间共用私聊历史')
        # Existing registered projects are intentional user-selected worktrees; never guess paths.
        registered = backend.project(data['backend_project'])
        if registered.get('node') != data['node']:
            raise ValueError('执行项目不属于所选节点')
        with self.connect() as db:
            agent = uid('agent')
            db.execute('INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout,configuring) VALUES(?,?,?,?,?,?,?,?,?,1)',
                       (agent,data['project'],name,user,data['node'],data['backend_project'],data['model'],data['effort'],int(data['timeout'])))
            self.log(db,user,'add_agent',{'agent':agent,'project':data['project']})
        try:
            backend.configure(data['backend_project'],data['model'],data['effort'],int(data['timeout']))
        except Exception:
            # Preserve the mapping when the remote outcome is uncertain; never reuse its history.
            raise
        with self.connect() as db: db.execute('UPDATE agents SET configuring=0 WHERE id=?',(agent,))
        return {'id':agent}

    def set_admin(self,user,project,agent):
        with self.connect() as db:
            row=self.require_member(db,project,user)
            if row['owner_id'] != user:
                raise PermissionError('只有群主可以指定管理员')
            if not db.execute('SELECT 1 FROM agents WHERE id=? AND project_id=?',(agent,project)).fetchone():
                raise ValueError('agent 不在本项目')
            db.execute('UPDATE projects SET admin_agent_id=? WHERE id=?',(agent,project))
            self.log(db,user,'set_admin',{'project':project,'agent':agent})

    def configure_agent(self,user,data,backend):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            agent=self.require_agent_owner(db,data['agent'],user)
            if db.execute("SELECT 1 FROM tasks WHERE agent_id=? AND state IN ('queued','starting','running','unknown')",(agent['id'],)).fetchone():
                raise ValueError('请先等 agent 本轮结束；可暂停项目阻止新轮次')
            db.execute('UPDATE agents SET configuring=1 WHERE id=?',(agent['id'],))
        backend.configure(agent['backend_project'],data['model'],data['effort'],int(data['timeout']))
        with self.connect() as db:
            db.execute('UPDATE agents SET model=?,effort=?,timeout=?,enabled=?,configuring=0 WHERE id=?',(data['model'],data['effort'],int(data['timeout']),int(bool(data['enabled'])),agent['id']))
            self.log(db,user,'configure_agent',{'agent':agent['id'],'model':data['model'],'effort':data['effort']})
        return {'updated':agent['id']}

    def message(self,user,project,body,recipient=None):
        with self.connect() as db:
            self.require_member(db,project,user)
            return self._message(db,project,user,'human',body,recipient)

    def _message(self,db,project,sender,kind,body,recipient=None,causal_task=None):
        if not isinstance(body,str) or not 1 <= len(body.strip()) <= 16000:
            raise ValueError('消息不能为空，最多 16000 字符')
        if recipient and not (db.execute('SELECT 1 FROM agents WHERE id=? AND project_id=?',(recipient,project)).fetchone()
                or db.execute('SELECT 1 FROM project_users WHERE user_id=? AND project_id=?',(recipient,project)).fetchone()):
            raise ValueError('收件人必须在本项目')
        cursor=db.execute('INSERT INTO messages(project_id,sender_id,sender_type,recipient_id,body,created_at,causal_task) VALUES(?,?,?,?,?,?,?)',
                          (project,sender,kind,recipient,body.strip(),time.time(),causal_task))
        return {'id':cursor.lastrowid}

    def visible_messages(self, agent, after=0, cutoff=None):
        with self.connect() as db:
            row=db.execute('SELECT * FROM agents WHERE id=?',(agent,)).fetchone()
            if not row:
                raise ValueError('未知 agent')
            return [dict(x) for x in db.execute('SELECT * FROM messages WHERE project_id=? AND id>? AND id<=? AND (recipient_id IS NULL OR recipient_id=? OR sender_id=?) ORDER BY id',
                        (row['project_id'],after,cutoff or 9223372036854775807,agent,agent)).fetchall()]

    def task(self,user,data):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project=self.require_member(db,data['project'],user)
            agent=self.require_agent_owner(db,data['agent'],user)
            if agent['project_id']!=data['project'] or not agent['enabled']:
                raise ValueError('请选择可用的项目 agent')
            return self._task(db,project,agent,data,user)

    def _task(self,db,project,agent,data,actor):
        if agent.get('configuring'): raise ValueError('机器配置尚未确认；请所有者重新确认设置')
        if project['paused']:
            raise ValueError('项目已暂停')
        if project['budget_remaining'] <= 0:
            raise ValueError('自动协作轮次已用完，请群主恢复额度')
        stage=data.get('stage','discuss')
        if stage not in {'discuss','review','implement'}:
            raise ValueError('未知任务阶段')
        if stage=='implement' and not data.get('allowed_files','').strip():
            raise ValueError('实现任务需要明确文件所有权')
        for key in ('title','instructions','acceptance','validation'):
            if not isinstance(data.get(key),str) or not data[key].strip() or len(data[key])>16000:
                raise ValueError('任务目标、验收与验证不能为空且不得过长')
        if db.execute("SELECT 1 FROM tasks WHERE agent_id=? AND state IN ('queued','starting','running','unknown')",(agent['id'],)).fetchone():
            raise ValueError('这个 agent 已有活动任务')
        task=uid('task')
        cutoff=db.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE project_id=?',(project['id'],)).fetchone()[0]
        db.execute('INSERT INTO tasks(id,project_id,agent_id,title,contract,state,created_at,cutoff) VALUES(?,?,?,?,?,?,?,?)',
                   (task,project['id'],agent['id'],data['title'],json.dumps(data,ensure_ascii=False),'queued',time.time(),cutoff))
        db.execute('UPDATE projects SET budget_remaining=budget_remaining-1 WHERE id=?',(project['id'],))
        self.log(db,actor,'task',{'task':task,'agent':agent['id']})
        return {'id':task,'state':'queued'}

    def snapshot(self,user):
        from common import read_json
        with self.connect() as db:
            projects=[dict(x) for x in db.execute('SELECT p.* FROM projects p JOIN project_users m ON p.id=m.project_id WHERE m.user_id=?',(user,))]
            nodes=[]
            for n in db.execute('SELECT * FROM nodes WHERE owner_id=?',(user,)):
                config=read_json(self.root/'nodes'/(n['name']+'.json'),{})
                nodes.append({**dict(n),**{k:v for k,v in config.items() if k in {'host','platform'}}})
            # People cannot enumerate unrelated users/projects.
            people=[dict(x) for x in db.execute('SELECT DISTINCT u.id,u.username FROM users u JOIN project_users a ON u.id=a.user_id JOIN project_users b ON a.project_id=b.project_id WHERE b.user_id=?',(user,))]
            return {'projects':projects,'nodes':nodes,'users':people}

    def project_view(self,user,project):
        with self.connect() as db:
            p=self.require_member(db,project,user)
            members=[]
            for row in db.execute('SELECT * FROM agents WHERE project_id=?',(project,)):
                item=dict(row)
                busy=db.execute("SELECT state FROM tasks WHERE agent_id=? AND state IN ('queued','starting','running','unknown')",(row['id'],)).fetchone()
                item.update(role='admin' if p['admin_agent_id']==row['id'] else 'peer',status='configuration_pending' if row['configuring'] else busy['state'] if busy else ('idle' if row['enabled'] else 'disabled'))
                members.append(item)
            messages=[]
            for row in db.execute('SELECT * FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 500',(project,)).fetchall()[::-1]:
                item=dict(row)
                table='users' if item['sender_type']=='human' else 'agents'
                key='username' if table=='users' else 'name'
                sender=db.execute(f'SELECT {key} FROM {table} WHERE id=?',(item['sender_id'],)).fetchone() if item['sender_type']!='system' else None
                recipient=db.execute('SELECT name FROM agents WHERE id=?',(item['recipient_id'],)).fetchone()
                if not recipient: recipient=db.execute('SELECT username FROM users WHERE id=?',(item['recipient_id'],)).fetchone()
                item.update(sender_name=sender[0] if sender else '系统',recipient_name=recipient[0] if recipient else None,channel='direct' if item['recipient_id'] else 'group')
                messages.append(item)
            tasks=[dict(x) for x in db.execute('SELECT * FROM tasks WHERE project_id=? ORDER BY created_at DESC LIMIT 100',(project,))]
            executions=[dict(x) for x in db.execute('SELECT * FROM executions WHERE project_id=? ORDER BY created_at DESC LIMIT 100',(project,))]
            people=[dict(x) for x in db.execute('SELECT u.id,u.username FROM users u JOIN project_users p ON u.id=p.user_id WHERE p.project_id=?',(project,))]
            p['owner_name']=next((x['username'] for x in people if x['id']==p['owner_id']),p['owner_id'])
            p['admin_agent_name']=next((x['name'] for x in members if x['id']==p['admin_agent_id']),None)
            return {'project':p,'members':members,'messages':messages,'tasks':tasks,'executions':executions,'users':people,'budget_remaining':p['budget_remaining']}
