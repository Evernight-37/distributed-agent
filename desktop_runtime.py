"""Desktop controls for an independent, locally owned worker service."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import webbrowser
from pathlib import Path

from common import lock, read_json
from pull_worker import NoRedirect


def assets():
    return Path(sys.executable).parent / 'client' if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


def python_command():
    return str(Path(sys.executable).parent / 'runtime' / 'python.exe') if getattr(sys, 'frozen', False) else sys.executable


class DesktopRuntime:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.config_path = self.root / 'pull-config.json'

    def _control(self, operation, payload=None):
        info = read_json(self.root / 'desktop-service.json', {})
        if not info: raise RuntimeError('请先连接这台机器')
        from urllib.parse import urlsplit
        address = urlsplit(info['url'])
        if address.scheme != 'http' or address.hostname != '127.0.0.1' or not address.port or address.path or address.query or address.fragment or address.username:
            raise RuntimeError('本机服务地址无效')
        request = urllib.request.Request(info['url'] + '/desktop/' + operation,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'X-Desktop-Key': info['key'], 'Origin': info['url'], 'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=45 if payload is not None and operation == 'action' else 3) as response: result = json.load(response)
        except urllib.error.HTTPError as exc:
            try: error = json.load(exc).get('error', '本机操作失败')
            except (ValueError, AttributeError): error = '本机操作失败'
            raise RuntimeError(error) from None
        if not result.get('ok'): raise RuntimeError(result.get('error', '本机操作失败'))
        if result.get('instance') != info['instance']: raise RuntimeError('本机服务身份已改变，请重新打开应用')
        return result.get('result', {})

    def status(self):
        config = read_json(self.config_path, {})
        value = {'paired': bool(config), 'running': False, 'online': False,
                 'node': config.get('node', ''), 'server': config.get('server', '')}
        try: value.update(self._control('status'))
        except (OSError, ValueError, RuntimeError): pass
        return value

    def pair(self, server, code):
        if self.config_path.exists(): raise ValueError('这台机器已配对，请保留原身份并点启动')
        if not code.strip(): raise ValueError('请填写一次性配对码')
        self.root.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([python_command(), '-X', 'utf8', str(assets() / 'desktop_service.py'), 'pair', str(self.root)],
            input=json.dumps({'server': server.strip(), 'code': code.strip()}),
            capture_output=True, text=True, encoding='utf-8', timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try: value = json.loads(result.stdout)
        except ValueError: raise RuntimeError('配对程序无法运行，请检查安装是否完整') from None
        if not value.get('ok'): raise RuntimeError(value.get('error', '配对失败'))
        return value.get('result')

    def start(self):
        if not self.config_path.exists(): raise ValueError('请先填写地址和配对码，连接这台机器')
        self.root.mkdir(parents=True, exist_ok=True)
        with lock(self.root / 'desktop-start.lock'):
            existing = self.status()
            if existing.get('stopping'): raise RuntimeError('正在停止接收，请稍后再启动')
            if existing['running']: return existing
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == 'nt' else 0
            with (self.root / 'desktop-service.stdout.log').open('ab') as out, (self.root / 'desktop-service.stderr.log').open('ab') as err:
                process = subprocess.Popen([python_command(), '-X', 'utf8', str(assets() / 'desktop_service.py'), 'serve', str(self.root)],
                    cwd=assets(), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    creationflags=flags, start_new_session=os.name != 'nt')
            threading.Thread(target=process.wait, daemon=True).start()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                status = self.status()
                if status['running']: return status
                if process.poll() is not None: raise RuntimeError('后台接收服务启动失败。请保留已有账户，查看 desktop-service.stderr.log 启动日志；不要重复创建账户')
                time.sleep(.15)
            raise RuntimeError('服务还未就绪。若原执行端仍在运行，请先停止它；不要重新配对')

    def stop(self):
        if self.status()['running']: self._control('stop', {})
        return {'stopping': True}

    def fetch_state(self):
        return self._control('state')

    def register_project(self, name, path, model, effort):
        return self._control('action', {'action': 'register', 'name': name, 'path': path, 'model': model, 'effort': effort})

    def save_binding(self, data):
        return self._control('action', dict(data, action='binding'))

    def open_panel(self):
        ticket = self._control('browser', {})['ticket']
        webbrowser.open(read_json(self.root / 'desktop-service.json')['url'] + '/launch/' + ticket)

    def open_workbench(self, server=None):
        from pull_worker import validate_server
        server = server or self.status()['server']
        validate_server(server)
        if server.rstrip('/') == 'http://127.0.0.1:18876':
            raise ValueError('这个地址是 4090 的设备通道。请在 5090 打开工作台，或使用团队提供的 HTTPS 工作台地址')
        webbrowser.open(server)

    def start_workbench(self, open_browser=True):
        """Host an ordinary loopback workbench, without changing network access."""
        url = 'http://127.0.0.1:8765'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        def ready():
            try:
                with opener.open(url + '/api/session', timeout=1) as response: value = json.load(response)
                return 'bootstrap_required' in value and 'csrf' in value
            except (OSError, ValueError): return False
        if not ready():
            state = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AgentHarness' / 'shared-data'
            state.mkdir(parents=True, exist_ok=True)
            with lock(state / 'desktop-host-start.lock'):
                if not ready():
                    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == 'nt' else 0
                    with (state / 'panel.stdout.log').open('ab') as out, (state / 'panel.stderr.log').open('ab') as err:
                        process = subprocess.Popen([python_command(), '-X', 'utf8', str(assets() / 'panel.py'), '--state', str(state)],
                            cwd=assets(), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                            creationflags=flags, start_new_session=os.name != 'nt')
                    threading.Thread(target=process.wait, daemon=True).start()
                    deadline = time.monotonic() + 8
                    while not ready():
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError('无法启动工作台，8765 端口可能被其他软件使用')
                        time.sleep(.15)
        if open_browser: webbrowser.open(url)
        return url

