"""Authenticated project workgroups; standard-library HTTP service."""
import argparse
import json
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from collaboration_store import Store, digest
from coordinator import Coordinator
from controller import Harness, DEFAULT_STATE, SOURCE
from common import lock


def action(store, user, data, backend):
    who, op = user['id'], data['action']
    if op == 'create-project': return store.project_create(who, data['name'])
    if op == 'invite': return store.invite(who, data['project'])
    if op == 'join-project': return store.join(who, data['invite'])
    if op == 'add-agent': return store.add_agent(who, data, backend)
    if op == 'set-admin': return store.set_admin(who, data['project'], data['agent']) or {'updated': True}
    if op == 'configure-agent': return store.configure_agent(who, data, backend)
    if op == 'message': return store.message(who, data['project'], data['body'], data.get('recipient_id'))
    if op == 'task': return store.task(who, data)
    if op in {'pause-project', 'renew-budget'}:
        with store.connect() as db:
            project = store.require_member(db, data['project'], who)
            if project['owner_id'] != who: raise PermissionError('只有群主可以暂停或恢复额度')
            if op == 'pause-project':
                if not isinstance(data['paused'], bool): raise ValueError('paused 必须为布尔值')
                db.execute('UPDATE projects SET paused=? WHERE id=?', (int(data['paused']), project['id']))
            else:
                budget = int(data.get('budget', 20))
                if not 1 <= budget <= 200: raise ValueError('额度为 1–200 轮')
                db.execute('UPDATE projects SET budget_remaining=? WHERE id=?', (budget, project['id']))
            store.log(db, who, op, {'project': project['id']})
        return {'updated': True}
    if op == 'refresh-task':
        with store.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (data['task'],)).fetchone()
            if not row: raise ValueError('未知任务')
            store.require_member(db, row['project_id'], who)
            return dict(row)
    if op == 'cancel-task':
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (data['task'],)).fetchone()
            if not row: raise ValueError('未知任务')
            store.require_agent_owner(db, row['agent_id'], who)
            if row['state'] == 'queued':
                db.execute("UPDATE tasks SET state='cancelled',processed=1 WHERE id=?", (row['id'],))
                return {'status': 'cancelled'}
            if row['state'] not in {'starting','running','unknown'}: return {'status':row['state']}
            db.execute('UPDATE tasks SET cancel_requested=1 WHERE id=?',(row['id'],))
            store.log(db,who,op,{'task':row['id']})
        return {'status':'cancellation_requested'}
    if op == 'templates':
        with store.connect() as db: agent = store.require_agent_owner(db, data['agent'], who)
        return Harness(store.root, agent['node']).rpc({'action': 'execution_templates', 'project': agent['backend_project'], 'templates': data['templates']})
    if op in {'execution-approve', 'execution-cancel'}:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM executions WHERE id=?', (data['execution'],)).fetchone()
            if not row: raise ValueError('未知后台作业')
            agent = store.require_agent_owner(db, row['agent_id'], who)
            if op == 'execution-approve':
                if row['state'] != 'awaiting_owner': raise ValueError('作业不在待批准状态')
                db.execute("UPDATE executions SET state='approved' WHERE id=?", (row['id'],))
                store.log(db, who, op, {'execution': row['id']})
                return {'status': 'approved'}
            if row['state'] in {'awaiting_owner', 'approved'}:
                db.execute("UPDATE executions SET state='cancelled',notified=1 WHERE id=?", (row['id'],))
                return {'status': 'cancelled'}
            if row['state'] not in {'starting','running','unknown'}: return {'status':row['state']}
            db.execute('UPDATE executions SET cancel_requested=1 WHERE id=?',(row['id'],))
            store.log(db,who,op,{'execution':row['id']})
        return {'status':'cancellation_requested'}
    raise ValueError('未知操作')


def handler_for(root, backend=None, public_origin=None):
    store, backend = Store(root), backend or Harness(root)
    attempts, attempt_lock = defaultdict(deque), threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args): pass
        def origin(self): return public_origin or f'http://127.0.0.1:{self.server.server_port}'
        def allowed(self):
            return self.headers.get('Host') == urlsplit(self.origin()).netloc and self.headers.get('Sec-Fetch-Site') != 'cross-site'

        def session(self):
            cookie = SimpleCookie()
            try: cookie.load(self.headers.get('Cookie', ''))
            except Exception: pass
            token = cookie['harness_session'].value if 'harness_session' in cookie else None
            self.current = store.session(token)
            return self.current

        def send(self, status, data, content_type='application/json; charset=utf-8'):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if getattr(self, 'current', None):
                secure = '; Secure' if self.origin().startswith('https://') else ''
                self.send_header('Set-Cookie', 'harness_session='+self.current[0]+'; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400'+secure)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed(): return self.send(403, {'error': 'Host/site rejected'})
            try:
                path = urlsplit(self.path)
                token, csrf, user = self.session()
                if path.path == '/': return self.send(200, (SOURCE/'web'/'index.html').read_bytes(), 'text/html; charset=utf-8')
                if path.path == '/api/session': return self.send(200, {'csrf': csrf, 'user': user, 'bootstrap_required': store.bootstrap_required()})
                if not user: return self.send(401, {'error': '请先登录'})
                if path.path == '/api/snapshot': return self.send(200, {**store.snapshot(user['id']), 'user': user})
                if path.path == '/api/project': return self.send(200, store.project_view(user['id'], parse_qs(path.query).get('id', [''])[0]))
                return self.send(404, {'error': 'Not found'})
            except PermissionError as exc: self.send(403, {'error': str(exc)})
            except (ValueError, KeyError) as exc: self.send(400, {'error': str(exc)})
            except Exception: self.send(500, {'error': '后台操作失败，请检查服务日志'})

        def do_POST(self):
            if not self.allowed() or self.headers.get('Origin') != self.origin(): return self.send(403, {'error': 'Origin rejected'})
            try:
                token, csrf, user = self.session()
                if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf): raise PermissionError('CSRF rejected')
                if self.path != '/api/action' or self.headers.get('Content-Type', '').split(';')[0] != 'application/json': raise ValueError('Expected JSON action')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 262144: raise ValueError('Invalid body size')
                data = json.loads(self.rfile.read(size).decode('utf-8'))
                if not isinstance(data, dict): raise ValueError('Expected object')
                op = data['action']
                if op in {'login', 'bootstrap', 'register-user'}:
                    with attempt_lock:
                        bucket, now = attempts[self.client_address[0]], time.monotonic()
                        while bucket and bucket[0] < now-60: bucket.popleft()
                        if len(bucket) >= 10: return self.send(429, {'error': '登录操作过于频繁，请稍后重试'})
                        bucket.append(now)
                    if op != 'login':
                        created = store.signup(data['username'], data['password'], bootstrap=op=='bootstrap', invite=data.get('invite'))
                        if op == 'bootstrap': store.import_legacy(created['id'])
                    self.current = store.authenticate(data['username'], data['password'], token)
                    return self.send(200, {'user': self.current[2], 'csrf': self.current[1]})
                if not user: return self.send(401, {'error': '请先登录'})
                if op == 'logout':
                    with store.connect() as db: db.execute('DELETE FROM sessions WHERE id=?', (digest(token),))
                    self.current = store.session()
                    return self.send(200, {'user': None, 'csrf': self.current[1]})
                return self.send(200, action(store, user, data, backend))
            except PermissionError as exc: self.send(403, {'error': str(exc)})
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc: self.send(400, {'error': str(exc)})
            except Exception: self.send(500, {'error': '后台操作失败，请检查服务日志'})
    Handler.store = store
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', default=str(DEFAULT_STATE))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--origin', help='HTTPS reverse-proxy origin; backend remains loopback-only')
    args = parser.parse_args()
    if args.origin:
        parsed = urlsplit(args.origin)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username: parser.error('--origin requires an HTTPS origin without path or credentials')
    with lock(Path(args.state)/'coordinator.lock'):
        handler = handler_for(args.state, public_origin=args.origin)
        coordinator = Coordinator(handler.store)
        threading.Thread(target=coordinator.run, daemon=True).start()
        server = ThreadingHTTPServer(('127.0.0.1', args.port), handler)
        try: server.serve_forever()
        finally:
            coordinator.stop_event.set()
            server.server_close()


if __name__ == '__main__': main()
