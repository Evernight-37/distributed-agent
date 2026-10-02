"""Local application lifecycle; never stop an unrelated listener or agent job."""
import json
import os
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from common import lock, read_json
from desktop_runtime import DesktopRuntime


def split_windows_command(command):
    import ctypes
    from ctypes import wintypes
    count = ctypes.c_int()
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    argv = parse(command, ctypes.byref(count))
    if not argv: return []
    try: return [argv[i] for i in range(count.value)]
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free(argv)


def legacy_matches(record, state, argv):
    """Recognize only the old app's bundled Python hosting this exact state."""
    executable = Path(record.get('executable') or '').resolve()
    bundle = executable.parent.parent
    if executable.name.lower() != 'python.exe' or executable.parent.name.lower() != 'runtime': return False
    if not (bundle/'AgentHarness.exe').is_file() or not (bundle/'client/panel.py').is_file(): return False
    if len(argv) != 6 or argv[1:3] != ['-X', 'utf8'] or argv[4] != '--state': return False
    return Path(argv[0]).resolve() == executable and Path(argv[3]).resolve() == (bundle/'client/panel.py').resolve() and Path(argv[5]).resolve() == Path(state).resolve()


def legacy_host(state):
    if os.name != 'nt': return None
    # Query only the loopback 8765 listener; never enumerate other command lines.
    script = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $item = Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object -First 1; if ($item) { $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $item.OwningProcess); @{pid=$p.ProcessId; executable=$p.ExecutablePath; command=$p.CommandLine; stamp=$p.CreationDate.ToUniversalTime().Ticks.ToString()} | ConvertTo-Json -Compress }"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
    try: record = json.loads(result.stdout.decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        try: record = json.loads(result.stdout.decode('mbcs'))
        except (ValueError, UnicodeError): return None
    if not legacy_matches(record, state, split_windows_command(record.get('command') or '')): return None
    return {key: record[key] for key in ('pid', 'executable', 'stamp')}


def stop_legacy_host(state, expected):
    # Recheck ownership, process start time and executable immediately before stop.
    if legacy_host(state) != expected: raise RuntimeError('旧版服务身份已变化，请重新核对后退出')
    script = "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); $v = [Console]::In.ReadToEnd() | ConvertFrom-Json; $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$v.pid); if (!$p) { exit 0 }; if ($p.ExecutablePath -ne $v.executable -or $p.CreationDate.ToUniversalTime().Ticks.ToString() -ne $v.stamp) { exit 2 }; Stop-Process -Id $p.ProcessId -ErrorAction Stop"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], input=json.dumps(expected).encode('utf-8'), capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode: raise RuntimeError('旧版本机服务未能停止，窗口将保留')
    with lock(Path(state)/'coordinator.lock'): pass


class HostRuntime(DesktopRuntime):
    def __init__(self, root):
        super().__init__(root)
        self.control_path = self.root/'desktop-host.json'

    def stop_and_wait(self, timeout=60):
        self.stop()
        deadline = time.monotonic() + timeout
        while self.status()['running']:
            if time.monotonic() >= deadline: raise RuntimeError('本机协作服务尚未退出，请稍后重试')
            time.sleep(.1)
        with lock(self.root/'coordinator.lock'): pass


class Lifecycle:
    def __init__(self, root, host_root=None):
        self.root = Path(root)
        # Match the host directory selected by the desktop runtime.  The
        # optional argument keeps source and older callers backwards compatible.
        self.host_root = (Path(host_root).expanduser().resolve() if host_root else
                          Path(os.environ.get('LOCALAPPDATA', Path.home()))/'AgentHarness'/'shared-data')

    def receivers(self):
        return [DesktopRuntime(path) for path in (self.root/'receivers').glob('device-*') if path.is_dir()]

    def active_jobs(self):
        for runtime in self.receivers():
            for pattern in ('jobs/*/state.json', 'projects/*/executions/*/state.json'):
                for path in runtime.root.glob(pattern):
                    if read_json(path, {}).get('status') in {'starting', 'running', 'unknown', 'launching'}: return True
        return False

    def status(self):
        host = HostRuntime(self.host_root).status()
        legacy = None if host['running'] else legacy_host(self.host_root)
        unmanaged = False
        if not host['running'] and not legacy:
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8765/api/session', timeout=.5) as response:
                    unmanaged = response.status == 200
            except (OSError, ValueError): pass
        return {'receivers': [{'running': r.status()['running']} for r in self.receivers()],
                'host': {'running': host['running'] or bool(legacy) or unmanaged, 'managed': host['running'], 'legacy': bool(legacy)},
                'active_jobs': self.active_jobs()}

    def stop(self):
        errors = []
        receiver_paths = sorted(path for path in (self.root/'receivers').glob('device-*') if path.is_dir())
        def stop_receiver(item):
            index, path = item
            try:
                runtime = DesktopRuntime(path)
                runtime.stop_and_wait()
                return None
            except Exception as exc:
                return ('接收服务 ' + str(index), exc)
        if receiver_paths:
            # Each runtime has its own bounded wait.  Stop them concurrently so
            # one slow receiver cannot multiply the full-exit delay by the
            # number of paired machines.  map preserves stable error ordering.
            with ThreadPoolExecutor(max_workers=min(8, len(receiver_paths)),
                                    thread_name_prefix='stop-receiver') as pool:
                errors.extend(error for error in pool.map(stop_receiver, enumerate(receiver_paths, 1)) if error)
        try:
            host = HostRuntime(self.host_root)
            if host.status()['running']:
                host.stop_and_wait()
            else:
                legacy = legacy_host(self.host_root)
                if legacy: stop_legacy_host(self.host_root, legacy)
        except Exception as exc:
            errors.append(('本机协作服务', exc))
        if errors:
            # Bound both the number and length of external error strings.  The
            # user needs actionable service names without an unbounded process
            # or OS error being injected into the window.
            details = '; '.join(f'{name}: {str(exc).strip()[:240] or type(exc).__name__}'
                                for name, exc in errors[:8])
            omitted = len(errors) - 8
            if omitted:
                details += f'; 另有 {omitted} 个服务未能停止'
            raise RuntimeError('部分后台服务未能完全停止。' + details)
        return {'stopped': True, 'jobs_continue': self.active_jobs()}
