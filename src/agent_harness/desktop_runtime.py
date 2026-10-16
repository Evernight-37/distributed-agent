"""Desktop controls for an independent, locally owned worker service."""
import json
import hashlib
import os
import subprocess
import sys
import threading
import time
import shutil
import urllib.request
import urllib.error
import webbrowser
from pathlib import Path

from common import lock, read_json, write_json
from pull_worker import NoRedirect, deploy_runtime

SERVICE_RUNTIME_FILES = ('desktop_service.py', 'local_panel.py', 'pull_worker.py', 'common.py',
                         'worker.py', 'warm_worker.py', 'jobrunner.py', 'appserver_client.py',
                         'owner_worker.py', 'owner_backend.py', 'account_identity.py', 'VERSION')


def reconcile_orphaned_turns(root):
    """Close non-terminal model jobs that no project ownership record can reach."""
    root = Path(root)
    with lock(root / 'registry.lock'):
        owned = {record['job'] for path in (root / 'projects').glob('*/active.json')
                 if (record := read_json(path)) and record.get('job')}
        for path in (root / 'jobs').glob('*/state.json'):
            state = read_json(path, {})
            if state.get('status') in {'starting', 'running', 'launching', 'unknown'} and state.get('job') not in owned:
                state.update(status='interrupted', finished_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                             error='No active worker owns this non-terminal job; start a new turn explicitly')
                write_json(path, state)


def assets():
    if getattr(sys, 'frozen', False):
        from service_install import installed_bundle
        return installed_bundle() / 'client'
    return Path(__file__).resolve().parent


def web_assets():
    root = assets()
    return root / 'web' if (root / 'web').is_dir() else root.parents[1] / 'assets' / 'web'


def python_command():
    if getattr(sys, 'frozen', False):
        from service_install import installed_bundle
        return str(installed_bundle() / 'runtime' / 'python.exe')
    return sys.executable


class DesktopRuntime:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.config_path = self.root / 'pull-config.json'
        self.control_path = self.root / 'desktop-service.json'

    def _control(self, operation, payload=None):
        info = read_json(self.control_path, {})
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

    def _service_snapshot(self):
        """Create/reuse an immutable code directory for one receiver version."""
        source = assets()
        digest = hashlib.sha256()
        for name in SERVICE_RUNTIME_FILES:
            path = source / name
            digest.update(name.encode()); digest.update(path.read_bytes() if path.is_file() else b'')
        snapshot = self.root / 'service-runtime' / digest.hexdigest()[:16]
        if not snapshot.is_dir():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            temporary = snapshot.with_name(snapshot.name + '.tmp-' + str(os.getpid()))
            if temporary.exists(): shutil.rmtree(temporary)
            temporary.mkdir()
            for name in SERVICE_RUNTIME_FILES:
                source_file = source / name
                if source_file.is_file(): shutil.copyfile(source_file, temporary / name)
            try: os.replace(temporary, snapshot)
            except FileExistsError: shutil.rmtree(temporary)
        return snapshot

    def start(self):
        if not self.config_path.exists(): raise ValueError('请先填写地址和配对码，连接这台机器')
        self.root.mkdir(parents=True, exist_ok=True)
        with lock(self.root / 'desktop-start.lock'):
            snapshot = self._service_snapshot()
            existing = self.status()
            if existing.get('stopping'): raise RuntimeError('正在停止接收，请稍后再启动')
            if existing['running']:
                info = read_json(self.control_path, {})
                if info.get('assets') in {str(snapshot), str(assets())}: return existing
                self.stop_and_wait()
            # A paired receiver keeps its identity and project state across
            # app upgrades, but its executable Python files must come from the
            # currently installed bundle before the new service starts.
            reconcile_orphaned_turns(self.root)
            active = any(read_json(path) for path in (self.root / 'projects').glob('*/active.json'))
            if not active:
                deploy_runtime(self.root, assets())
            # Every receiver generation runs from an immutable snapshot. This
            # permits the observer service to restart while an old warm worker
            # is active, without overwriting files that process may use.
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == 'nt' else 0
            with (self.root / 'desktop-service.stdout.log').open('ab') as out, (self.root / 'desktop-service.stderr.log').open('ab') as err:
                process = subprocess.Popen([python_command(), '-X', 'utf8', str(snapshot / 'desktop_service.py'), 'serve', str(self.root)],
                    cwd=snapshot, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
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

    def stop_and_wait(self, timeout=60):
        self.stop()
        deadline = time.monotonic() + timeout
        while self.status()['running']:
            if time.monotonic() >= deadline:
                raise RuntimeError('后台服务仍在完成当前操作，窗口将保留。请稍后重试完全退出。')
            time.sleep(.1)
        with lock(self.root / 'pull-worker.lock'): pass
        return {'stopped': True}

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
        webbrowser.open(server)

    def start_workbench(self, open_browser=True, state_root=None):
        """Host an ordinary loopback workbench, without changing network access."""
        url = 'http://127.0.0.1:8765'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        def ready():
            try:
                with opener.open(url + '/api/session', timeout=1) as response: value = json.load(response)
                return 'bootstrap_required' in value and 'csrf' in value
            except (OSError, ValueError): return False
        if not ready():
            # Frozen desktop builds pass a directory beside the executable so the
            # portable app owns its service data.  Keep the historical per-user
            # location when called by source/older clients.
            state = (Path(state_root).expanduser().resolve() if state_root else
                     Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AgentHarness' / 'shared-data')
            state.mkdir(parents=True, exist_ok=True)
            with lock(state / 'desktop-host-start.lock'):
                if not ready():
                    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == 'nt' else 0
                    with (state / 'panel.stdout.log').open('ab') as out, (state / 'panel.stderr.log').open('ab') as err:
                        process = subprocess.Popen([python_command(), '-X', 'utf8', str(assets() / 'panel.py'), '--state', str(state), '--desktop-control'],
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

