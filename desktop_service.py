"""Hidden local receiver. No model calls until the owner enables a binding."""
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path

from common import identifier, lock, read_json, validate_project, write_json
from local_panel import handler_for
from pull_worker import Connector, enroll


def windows_user_sid():
    # whoami uses a Windows code page, even when Python runs with -X utf8.
    # Only the final ASCII SID field is needed; never decode the username.
    import subprocess
    output = subprocess.check_output(['whoami.exe', '/user', '/fo', 'csv', '/nh'])
    match = re.search(rb'"(S-1-[0-9]+(?:-[0-9]+)+)"\s*$', output)
    if not match:
        raise RuntimeError('无法确认当前 Windows 用户身份，接收服务未启动')
    return match.group(1).decode('ascii')


def serve(root):
    root = Path(root).resolve()
    connector = Connector(root / 'pull-config.json')
    stop = threading.Event()
    key, instance = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    browser_tickets, browser_sessions = {}, {}
    # Share the existing lock with CLI receivers. Never run two receivers.
    with lock(root / 'pull-worker.lock'):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(connector, 0))
        port = server.server_address[1]
        base = handler_for(connector, port)
        origin = f'http://127.0.0.1:{port}'

        class Handler(base):
            def desktop(self):
                if not self.valid_host() or not hmac.compare_digest(self.headers.get('X-Desktop-Key', ''), key):
                    self.respond({'ok': False, 'error': 'Local app authentication required'}, 403); return
                try:
                    route = self.path.removeprefix('/desktop/')
                    if self.command == 'POST':
                        if self.headers.get('Origin') != origin: raise ValueError('Local Origin required')
                        size = int(self.headers.get('Content-Length', '0'))
                        if not 0 < size <= 65536: raise ValueError('Invalid request size')
                        data = json.loads(self.rfile.read(size))
                    else: data = None
                    if route == 'status' and data is None:
                        state = read_json(root / 'connector-status.json', {})
                        result = {'running': True, 'stopping': stop.is_set(), 'online': bool(state.get('online')) and time.time() - state.get('updated', 0) < 20}
                    elif route == 'state' and data is None:
                        result = {'projects': [read_json(p) for p in (root / 'projects').glob('*/project.json')],
                                  'shared_projects': read_json(root / 'broker-projects.json', []), 'bindings': connector.bindings()}
                    elif route == 'action' and data is not None:
                        action = data.pop('action')
                        if action == 'binding': result = connector.save_binding(data)
                        elif action == 'approve-execution': result = connector.approve(data['job_id'], data['approved'], data.get('expected_template')) or {'updated': True}
                        elif action == 'register':
                            path = Path(data['path'])
                            if not path.is_absolute() or not path.is_dir(): raise ValueError('请选择已存在的项目文件夹')
                            project = validate_project({'name': identifier(data['name']), 'remote_path': str(path.resolve()),
                                'model': data['model'], 'effort': data['effort'], 'timeout': 120, 'local_control': True})
                            # Existing projects retain their session and memory.
                            existing = read_json(root / 'projects' / project['name'] / 'project.json')
                            if existing:
                                if Path(existing['remote_path']).resolve() != path.resolve(): raise ValueError('此本机项目名已对应其他目录，请换一个名字')
                                result = existing
                            else: result = connector.executor({'action': 'register', 'project': project, 'memory': {'revision': 0, 'entries': []}})
                        else: raise ValueError('Unknown desktop action')
                    elif route == 'stop' and data is not None:
                        stop.set(); result = {'stopping': True}
                    elif route == 'browser' and data is not None:
                        ticket = secrets.token_urlsafe(32)
                        browser_tickets.clear()
                        browser_tickets[ticket] = time.monotonic() + 30
                        result = {'ticket': ticket}
                    else: raise ValueError('Unknown desktop operation')
                    self.respond({'ok': True, 'instance': instance, 'result': result})
                except Exception as exc: self.respond({'ok': False, 'error': str(exc)}, 400)

            def do_GET(self):
                if self.path.startswith('/desktop/'): return self.desktop()
                if self.path.startswith('/launch/') and self.valid_host():
                    expiry = browser_tickets.pop(self.path.removeprefix('/launch/'), 0)
                    if expiry > time.monotonic():
                        session = secrets.token_urlsafe(32)
                        browser_sessions[session] = time.monotonic() + 86400
                        self.send_response(302)
                        self.send_header('Location', '/')
                        self.send_header('Set-Cookie', f'harness_local={session}; HttpOnly; SameSite=Strict; Path=/')
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Referrer-Policy', 'no-referrer')
                        self.send_header('Content-Length', '0'); self.end_headers(); return
                if not self.browser_auth(): return self.respond({'ok': False, 'error': '请从本机应用打开高级设置'}, 403)
                super().do_GET()

            def do_POST(self):
                if self.path.startswith('/desktop/'): return self.desktop()
                if not self.browser_auth(): return self.respond({'ok': False, 'error': '请从本机应用打开高级设置'}, 403)
                super().do_POST()

            def browser_auth(self):
                cookie = SimpleCookie()
                try: cookie.load(self.headers.get('Cookie', ''))
                except Exception: return False
                session = cookie.get('harness_local')
                return bool(session and browser_sessions.get(session.value, 0) > time.monotonic())

        server.RequestHandlerClass = Handler
        write_json(root / 'desktop-service.json', {'url': origin, 'key': key, 'instance': instance, 'pid': os.getpid()})
        if os.name != 'nt': (root / 'desktop-service.json').chmod(0o600)
        else:
            # Use the same ACL routine as enrollment without exposing the key.
            import subprocess
            sid = windows_user_sid()
            result = subprocess.run(['icacls.exe', str(root / 'desktop-service.json'), '/inheritance:r', '/grant:r', '*' + sid + ':F', '*S-1-5-18:F'], capture_output=True)
            if result.returncode: raise RuntimeError('本机服务权限设置失败')
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            while not stop.is_set():
                try: connector.cycle()
                except Exception as exc: write_json(root / 'connector-status.json', {'online': False, 'error': type(exc).__name__, 'updated': time.time()})
                else: write_json(root / 'connector-status.json', {'online': True, 'updated': time.time()})
                stop.wait(3)
        finally:
            server.shutdown(); server.server_close()
            write_json(root / 'connector-status.json', {'online': False, 'updated': time.time()})


if __name__ == '__main__':
    command, root = sys.argv[1:]
    if command == 'pair':
        try:
            data = json.load(sys.stdin)
            value = enroll(data['server'], data['code'], root)
            print(json.dumps({'ok': True, 'result': value}, ensure_ascii=False))
        except Exception as exc:
            error = str(exc)
            if isinstance(exc, urllib.error.HTTPError):
                try: error = json.load(exc).get('error', '服务拒绝配对，请重新生成配对码')
                except (ValueError, AttributeError): pass
            print(json.dumps({'ok': False, 'error': error}, ensure_ascii=False))
    elif command == 'serve': serve(root)
    else: raise ValueError('Unknown service command')
