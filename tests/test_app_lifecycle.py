"""Lifecycle contracts for packaged services and local shutdown."""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app_lifecycle import Lifecycle, legacy_matches
from host_control import managed_handler
from service_install import install_bundle


class ServiceInstallTests(unittest.TestCase):
    def test_versioned_install_reuses_valid_copy_and_keeps_old_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); bundle = root/'download'; destination = root/'installed'
            (bundle/'runtime').mkdir(parents=True); (bundle/'client').mkdir()
            (bundle/'runtime/python.exe').write_bytes(b'python')
            (bundle/'client/desktop_service.py').write_bytes(b'v1')
            first = install_bundle(bundle, destination)
            self.assertEqual(install_bundle(bundle, destination), first)
            self.assertEqual((first/'client/desktop_service.py').read_bytes(), b'v1')
            (bundle/'client/desktop_service.py').write_bytes(b'v2')
            second = install_bundle(bundle, destination)
            self.assertNotEqual(first, second)
            self.assertEqual((first/'client/desktop_service.py').read_bytes(), b'v1')
            self.assertEqual((second/'client/desktop_service.py').read_bytes(), b'v2')
            (second/'client/desktop_service.py').write_bytes(b'corrupt')
            with self.assertRaisesRegex(RuntimeError, '校验失败'): install_bundle(bundle, destination)

    def test_incomplete_bundle_does_not_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'bundle/runtime').mkdir(parents=True); (root/'bundle/client').mkdir()
            (root/'bundle/runtime/python.exe').write_bytes(b'python')
            with self.assertRaisesRegex(RuntimeError, '缺少后台入口'):
                install_bundle(root/'bundle', root/'installed')
            self.assertEqual(list((root/'installed').glob('*/.complete')), [])


class LegacyIdentityTests(unittest.TestCase):
    def test_only_owned_old_bundle_and_exact_state_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); bundle = root/'AgentHarness'; state = root/'owned-state'
            (bundle/'runtime').mkdir(parents=True); (bundle/'client').mkdir()
            for path in ('AgentHarness.exe', 'runtime/python.exe', 'client/panel.py'):
                (bundle/path).write_bytes(b'')
            exe = str(bundle/'runtime/python.exe'); panel = str(bundle/'client/panel.py')
            argv = [exe, '-X', 'utf8', panel, '--state', str(state)]
            self.assertTrue(legacy_matches({'executable': exe}, state, argv))
            self.assertFalse(legacy_matches({'executable': exe}, root/'other-state', argv))
            self.assertFalse(legacy_matches({'executable': exe}, state, argv[:-1]+[str(root/'other-state')]))
            self.assertFalse(legacy_matches({'executable': exe}, state, argv[:3]+[str(root/'other.py')]+argv[4:]))
            self.assertFalse(legacy_matches({'executable': str(root/'other/python.exe')}, state, argv))


class ManagedHostTests(unittest.TestCase):
    def test_control_rejects_wrong_key_and_origin_then_stops(self):
        class Base(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_GET(self): self.send(200, {'ordinary': True})
            def do_POST(self): self.send(200, {'ordinary': True})
        with tempfile.TemporaryDirectory() as directory:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Base)
            with mock.patch('host_control.protect'):
                server.RequestHandlerClass = managed_handler(Base, server, Path(directory)/'control.json')
            data = json.loads((Path(directory)/'control.json').read_text())
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            port = server.server_port
            def request(method, path, key='', origin=''):
                client = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                client.request(method, path, body=b'{}' if method=='POST' else None,
                    headers={'X-Desktop-Key':key,'Origin':origin,'Content-Length':'2' if method=='POST' else '0'})
                result = client.getresponse(); status = result.status; body = json.loads(result.read()); client.close()
                return status, body
            try:
                self.assertEqual(request('GET','/desktop/status')[0], 403)
                self.assertEqual(request('POST','/desktop/stop',data['key'],'https://foreign.example')[0], 403)
                self.assertTrue(request('GET','/desktop/status',data['key'])[1]['result']['running'])
                self.assertEqual(request('POST','/desktop/stop',data['key'],data['url'])[0], 200)
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            finally: server.shutdown(); server.server_close()


class ExitTests(unittest.TestCase):
    def test_active_project_execution_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root/'receivers/device-a/projects/project-a/executions/job-a/state.json'
            state.parent.mkdir(parents=True)
            state.write_text('{"status":"running"}', encoding='utf-8')
            self.assertTrue(Lifecycle(root).active_jobs())

    def test_complete_exit_stops_receivers_and_managed_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('device-a','device-b'): (root/'receivers'/name).mkdir(parents=True)
            with mock.patch('app_lifecycle.DesktopRuntime') as receiver_class, mock.patch('app_lifecycle.HostRuntime') as host_class, mock.patch('app_lifecycle.legacy_host', return_value=None):
                host_class.return_value.status.return_value = {'running': True}
                result = Lifecycle(root).stop()
                self.assertTrue(result['stopped'])
                self.assertEqual(receiver_class.return_value.stop_and_wait.call_count, 2)
                host_class.return_value.stop_and_wait.assert_called_once()

    def test_unmanaged_host_is_not_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch('app_lifecycle.HostRuntime') as host_class, mock.patch('app_lifecycle.legacy_host', return_value=None), mock.patch('app_lifecycle.stop_legacy_host') as stop_legacy:
                host_class.return_value.status.return_value = {'running': False}
                Lifecycle(directory).stop()
                host_class.return_value.stop_and_wait.assert_not_called()
                stop_legacy.assert_not_called()
