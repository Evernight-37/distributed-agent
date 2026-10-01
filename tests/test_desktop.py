"""Isolated contract tests for the owner-controlled desktop receiver."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import desktop_runtime
import desktop_service
from common import write_json


class FakeConnector:
    def __init__(self, config_path):
        self.root = Path(config_path).parent
        self.config = {"node": "test-node", "server": "http://127.0.0.1:1"}
        self._bindings = []

    def cycle(self):
        write_json(self.root / "broker-projects.json", [])

    def bindings(self):
        return list(self._bindings)

    def save_binding(self, value):
        self._bindings.append(dict(value))
        return value

    def executor(self, request):
        return request.get("project", {})


class DesktopServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_json(self.root / "pull-config.json", {
            "server": "http://127.0.0.1:1", "root": str(self.root),
            "device_token": "synthetic-token", "node": "test-node",
        })
        self.thread = threading.Thread(target=desktop_service.serve,
                                       args=(self.root,), daemon=True)
        self.connector_patch = mock.patch.object(desktop_service, "Connector", FakeConnector)
        self.connector_patch.start()
        self.thread.start()
        deadline = time.time() + 5
        while not (self.root / "desktop-service.json").exists() and time.time() < deadline:
            time.sleep(.02)
        self.assertTrue((self.root / "desktop-service.json").exists())
        self.info = json.loads((self.root / "desktop-service.json").read_text())
        self.host, self.port = "127.0.0.1", int(self.info["url"].rsplit(":", 1)[1])

    def tearDown(self):
        try:
            self.request("POST", "/desktop/stop", {}, key=self.info['key'], origin=self.info["url"])
        except Exception:
            pass
        self.thread.join(timeout=8)
        self.assertFalse(self.thread.is_alive(), 'Receiver must not leak after a test')
        self.connector_patch.stop()
        # Windows may release the receiver lock just after the serving thread
        # exits; retry cleanup instead of turning a successful contract test
        # into a file-handle race.
        for _ in range(20):
            try:
                self.tmp.cleanup()
                break
            except PermissionError:
                time.sleep(.05)

    def request(self, method, path, data=None, key=None, origin=None, cookie=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=3)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if key:
            headers["X-Desktop-Key"] = key
        if origin:
            headers["Origin"] = origin
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        result = json.loads(payload.decode()) if payload else None
        values = {k: response.getheader(k) for k in ("Set-Cookie", "Location")}
        conn.close()
        return response.status, result, values

    def test_start_is_single_receiver_and_status_hides_secret(self):
        runtime = desktop_runtime.DesktopRuntime(self.root)
        write_json(self.root / "desktop-service.json", self.info)
        first = runtime.status()
        with mock.patch.object(desktop_runtime.subprocess, 'Popen') as spawn:
            runtime.start(); runtime.start()
            spawn.assert_not_called()
        second = runtime.status()
        self.assertTrue(first["running"])
        self.assertEqual(first["node"], "test-node")
        self.assertEqual({k: first[k] for k in ("paired", "running", "node", "server")},
                         {k: second[k] for k in ("paired", "running", "node", "server")})
        self.assertNotIn("key", first)
        self.assertNotIn("device_token", first)

    def test_desktop_auth_browser_ticket_is_one_time_and_rejects_direct_browser(self):
        runtime = desktop_runtime.DesktopRuntime(self.root)
        ticket = runtime._control("browser", {})["ticket"]
        status, _, headers = self.request("GET", "/launch/" + ticket)
        self.assertEqual(status, 302)
        self.assertTrue(headers["Set-Cookie"].startswith("harness_local="))
        self.assertEqual(self.request("GET", "/launch/" + ticket)[0], 403)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("GET", "/api/state", cookie=cookie)[0], 200)
        self.assertEqual(self.request("GET", "/", cookie=None)[0], 403)
        self.assertEqual(self.request("GET", "/api/state")[0], 403)
        self.assertEqual(self.request("POST", "/api/action", {'action': 'binding'}, origin=self.info['url'])[0], 403)
        self.assertEqual(self.request("GET", "/desktop/status")[0], 403)

    def test_stop_eventually_reports_not_running(self):
        runtime = desktop_runtime.DesktopRuntime(self.root)
        self.assertTrue(runtime.status()["running"])
        runtime.stop()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())


class DesktopRuntimeContractTests(unittest.TestCase):
    def test_windows_sid_ignores_chinese_username_encoding(self):
        sid = 'S-1-5-21-123-456-789-1001'
        for encoding in ('gbk', 'utf-8'):
            output = ('"电脑\\中文用户","' + sid + '"\r\n').encode(encoding)
            with self.subTest(encoding=encoding), mock.patch('subprocess.check_output', return_value=output) as command:
                self.assertEqual(desktop_service.windows_user_sid(), sid)
                self.assertNotIn('text', command.call_args.kwargs)
                self.assertNotIn('encoding', command.call_args.kwargs)

    def test_windows_sid_rejects_missing_or_invalid_identity(self):
        for output in (b'', b'"user","not-a-sid"\r\n', b'"S-1-5-21-1001","invalid"\r\n'):
            with self.subTest(output=output), mock.patch('subprocess.check_output', return_value=output):
                with self.assertRaises(RuntimeError): desktop_service.windows_user_sid()

    def test_status_and_control_do_not_expose_service_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "pull-config.json", {"node": "n", "server": "https://broker.example"})
            write_json(root / "desktop-service.json", {"url": "http://127.0.0.1:1", "key": "secret", "instance": "i"})
            runtime = desktop_runtime.DesktopRuntime(root)
            with mock.patch.object(runtime, "_control", return_value={"running": False}):
                value = runtime.status()
            self.assertNotIn("secret", json.dumps(value))
            self.assertNotIn("key", value)

    def test_changed_local_address_does_not_send_a_key_to_remote_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / 'desktop-service.json', {'url': 'https://outside.example', 'key': 'private', 'instance': 'i'})
            with self.assertRaises(RuntimeError): desktop_runtime.DesktopRuntime(root)._control('status')


class DesktopGUIContractTests(unittest.TestCase):
    def test_save_registers_local_project_before_binding_and_keeps_owner_choices(self):
        import tkinter as tk
        from desktop_app import DesktopApp
        root = tk.Tk(); root.withdraw()
        self.addCleanup(root.destroy)
        with mock.patch('desktop_app.DesktopRuntime') as runtime_class, mock.patch.object(DesktopApp, 'refresh_status'):
            app = DesktopApp(root)
            self.assertIsInstance(runtime_class.call_args.args[0], Path)
            self.assertFalse(app.enable.get())
            runtime = runtime_class.return_value
            runtime.fetch_state.return_value = {'bindings': []}
            app.project_name.set('local-project'); app.project_path.set(str(Path(tempfile.gettempdir())))
            app.shared_by_label = {'Research': 'shared-id'}; app.shared_project.set('Research')
            app.model.set('owner-model'); app.effort.set('high'); app.enable.set(True)
            app._run = lambda fn, done=None: fn()
            app.save_project()
            runtime.register_project.assert_called_once_with('local-project', str(Path(tempfile.gettempdir())), 'owner-model', 'high')
            data = runtime.save_binding.call_args.args[0]
            self.assertEqual((data['project_id'], data['backend_project']), ('shared-id', 'local-project'))
            self.assertEqual(data['stages'], ['discuss'])
            self.assertEqual((data['model'], data['effort']), ('owner-model', 'high'))

    def test_existing_identity_starts_without_pairing_again(self):
        import tkinter as tk
        from desktop_app import DesktopApp
        root = tk.Tk(); root.withdraw(); self.addCleanup(root.destroy)
        with mock.patch('desktop_app.DesktopRuntime') as runtime_class, mock.patch.object(DesktopApp, 'refresh_status'):
            app = DesktopApp(root)
            runtime = runtime_class.return_value; runtime.status.return_value = {'paired': True}
            app._run = lambda fn, done=None: fn()
            app.pair_start()
            runtime.pair.assert_not_called(); runtime.start.assert_called_once()


class PackagedReceiverTests(unittest.TestCase):
    @unittest.skipUnless((Path(desktop_runtime.__file__).parent / 'dist/AgentHarness/runtime/python.exe').exists(), 'Build the portable app first')
    def test_sidecar_pair_register_bind_duplicate_start_and_stop(self):
        class Broker(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                result = {'node': 'portable-test'} if self.path.endswith('/enroll') else {'jobs': [], 'projects': [{'id': 'project-test', 'name': 'Research'}], 'cancellations': []}
                body = json.dumps({'ok': True, 'result': result}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        broker = ThreadingHTTPServer(('127.0.0.1', 0), Broker)
        threading.Thread(target=broker.serve_forever, daemon=True).start()
        self.addCleanup(broker.server_close); self.addCleanup(broker.shutdown)
        bundle = Path(desktop_runtime.__file__).parent / 'dist/AgentHarness'
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(desktop_runtime, 'python_command', return_value=str(bundle / 'runtime/python.exe')), mock.patch.object(desktop_runtime, 'assets', return_value=bundle / 'client'):
            root = Path(directory); runtime = desktop_runtime.DesktopRuntime(root / '中文用户数据')
            runtime.pair(f'http://127.0.0.1:{broker.server_address[1]}', 'synthetic-one-time-code')
            with self.assertRaises(ValueError): runtime.pair('http://127.0.0.1:1', 'again')
            try:
                self.assertTrue(runtime.start()['running'])
                instance = json.loads((runtime.root / 'desktop-service.json').read_text())['instance']
                runtime.start()
                self.assertEqual(instance, json.loads((runtime.root / 'desktop-service.json').read_text())['instance'])
                workdir = root / 'project'; workdir.mkdir()
                runtime.register_project('local-test', str(workdir), 'owner-model', 'high')
                runtime.save_binding({'project_id': 'project-test', 'backend_project': 'local-test', 'name': 'Test', 'model': 'owner-model', 'effort': 'high', 'timeout': 120, 'enabled': False, 'stages': ['discuss']})
                state = runtime.fetch_state()
                self.assertFalse(state['bindings'][0]['enabled'])
                self.assertEqual(state['bindings'][0]['model'], 'owner-model')
                self.assertNotIn('device_token', json.dumps(state))
            finally:
                runtime.stop_and_wait(timeout=20)
                self.assertFalse(runtime.status()['running'])


if __name__ == "__main__":
    unittest.main()
