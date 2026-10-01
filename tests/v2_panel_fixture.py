"""Loopback-only human console; no external libraries or credentials in responses."""
import argparse
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from controller import Harness, DEFAULT_STATE, SOURCE
from common import identifier


def handler_for(root):
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def allowed(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def send(self, status, data, content_type='application/json; charset=utf-8'):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {'error': 'Invalid Host'})
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                return self.send(403, {'error': 'Cross-site access rejected'})
            try:
                parsed = urlsplit(self.path)
                harness = Harness(root)
                if parsed.path == '/':
                    return self.send(200, (SOURCE / 'web' / 'index.html').read_bytes(), 'text/html; charset=utf-8')
                if parsed.path == '/api/session':
                    return self.send(200, {'token': token})
                if parsed.path == '/api/snapshot':
                    return self.send(200, harness.snapshot())
                if parsed.path == '/api/job':
                    job = parse_qs(parsed.query).get('id', [''])[0]
                    return self.send(200, harness.status(identifier(job), fetch=True))
                return self.send(404, {'error': 'Not found'})
            except Exception as exc:
                return self.send(400, {'error': str(exc)})

        def do_POST(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if not self.allowed() or self.headers.get('Origin') != origin or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), token):
                return self.send(403, {'error': 'Origin/token rejected'})
            if self.path != '/api/action' or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.send(400, {'error': 'Expected JSON action'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 262144:
                    raise ValueError('Invalid body size')
                data = json.loads(self.rfile.read(size).decode('utf-8'))
                harness = Harness(root)
                action = data['action']
                if action == 'submit':
                    if data['stage'] not in {'discuss', 'implement', 'review'}:
                        raise ValueError('Invalid stage')
                    result = harness.submit(data['project'], data['task'], data['stage'], data.get('allowed_files', ''), data['acceptance'], data['validation'])
                elif action == 'configure':
                    result = harness.configure(data['project'], data['model'], data['effort'], int(data['timeout']))
                elif action == 'register':
                    identifier(data['node'])
                    result = Harness(root, data['node']).register(data['name'], data['local_path'], data['remote_path'], data['model'], data['effort'], int(data['timeout']))
                elif action == 'cancel':
                    result = harness.rpc({'action': 'cancel', 'job': identifier(data['job'])})
                elif action == 'message':
                    result = harness.queue(data['project'], data['text'])
                elif action == 'review':
                    result = harness.review(data['job'], data['verdict'], data['evidence'])
                elif action == 'remember':
                    if data['kind'] not in {'fact', 'decision', 'question'}:
                        raise ValueError('Invalid memory kind')
                    result = harness.remember(data['project'], data['kind'], data['text'], data['evidence'])
                elif action == 'probe':
                    result = Harness(root, identifier(data['node'])).rpc({'action': 'probe'})
                else:
                    raise ValueError('Unknown action')
                self.send(200, result)
            except Exception as exc:
                self.send(400, {'error': str(exc)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=str(DEFAULT_STATE))
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(Path(args.state)))
    print(f'Agent Harness: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    raise SystemExit('Historical test fixture; use panel.py for the authenticated service')
