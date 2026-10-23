"""Isolated contract tests for the owner-controlled desktop receiver."""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import desktop_runtime
import desktop_service
from common import read_json, write_json
from service_install import runtime_python


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGED_APP = Path(os.environ.get("AGENT_HARNESS_TEST_BUNDLE", PROJECT_ROOT / "dist" / "AgentHarness"))


class FakeConnector:
    def __init__(self, config_path, transport=None, executor=None):
        self.root = Path(config_path).parent
        self.config = {"node": "test-node", "server": "http://127.0.0.1:1"}
        self._bindings = []
        if executor is not None:
            self.executor = executor

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
        write_json(
            self.root / "pull-config.json",
            {
                "server": "http://127.0.0.1:1",
                "root": str(self.root),
                "device_token": "synthetic-token",
                "node": "test-node",
            },
        )
        self.thread = threading.Thread(target=desktop_service.serve, args=(self.root,), daemon=True)
        self.connector_patch = mock.patch.object(desktop_service, "Connector", FakeConnector)
        self.runtime_patch = mock.patch.object(
            desktop_service,
            "receiver_runtime",
            lambda root: type("Runtime", (), {"rpc": lambda self, request: request.get("project", {})})(),
        )
        self.connector_patch.start()
        self.runtime_patch.start()
        self.thread.start()
        deadline = time.time() + 5
        while not (self.root / "desktop-service.json").exists() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.root / "desktop-service.json").exists())
        self.info = json.loads((self.root / "desktop-service.json").read_text())
        self.host, self.port = "127.0.0.1", int(self.info["url"].rsplit(":", 1)[1])

    def tearDown(self):
        try:
            self.request("POST", "/desktop/stop", {}, key=self.info["key"], origin=self.info["url"])
        except Exception:
            pass
        self.thread.join(timeout=8)
        self.assertFalse(self.thread.is_alive(), "Receiver must not leak after a test")
        self.connector_patch.stop()
        self.runtime_patch.stop()
        # Windows may release the receiver lock just after the serving thread
        # exits; retry cleanup instead of turning a successful contract test
        # into a file-handle race.
        for _ in range(20):
            try:
                self.tmp.cleanup()
                break
            except PermissionError:
                time.sleep(0.05)

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
        with mock.patch.object(desktop_runtime.subprocess, "Popen") as spawn:
            runtime.start()
            runtime.start()
            spawn.assert_not_called()
        second = runtime.status()
        self.assertTrue(first["running"])
        self.assertEqual(first["node"], "test-node")
        self.assertEqual(
            {k: first[k] for k in ("paired", "running", "node", "server")},
            {k: second[k] for k in ("paired", "running", "node", "server")},
        )
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
        self.assertEqual(self.request("POST", "/api/action", {"action": "binding"}, origin=self.info["url"])[0], 403)
        self.assertEqual(self.request("GET", "/desktop/status")[0], 403)

    def test_stop_eventually_reports_not_running(self):
        runtime = desktop_runtime.DesktopRuntime(self.root)
        self.assertTrue(runtime.status()["running"])
        runtime.stop()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())


class DesktopRuntimeContractTests(unittest.TestCase):
    def test_source_snapshot_contains_release_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = desktop_runtime.DesktopRuntime(Path(directory))._service_snapshot()
            expected = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8")
            self.assertEqual((snapshot / "VERSION").read_text(encoding="utf-8"), expected)
            self.assertTrue((snapshot / "release_info.py").is_file())

    def test_start_reconciles_orphan_before_runtime_deploy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "p"
            job = root / "jobs" / "orphan"
            project.mkdir(parents=True)
            job.mkdir(parents=True)
            write_json(project / "active.json", None)
            write_json(job / "state.json", {"job": "orphan", "project": "p", "status": "starting"})

            desktop_runtime.reconcile_orphaned_turns(root)

            state = read_json(job / "state.json")
            self.assertEqual(state["status"], "interrupted")
            self.assertIn("No active worker owns", state["error"])

    def test_active_worker_restart_uses_immutable_snapshot_without_deploy(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as source_dir:
            root, source = Path(directory), Path(source_dir)
            (root / "projects" / "p").mkdir(parents=True)
            write_json(root / "pull-config.json", {"node": "n", "server": "https://broker.example"})
            write_json(root / "projects" / "p" / "active.json", {"job": "j", "pid": 1, "stamp": "s"})
            # An older active generation keeps its receiver-root files intact.
            (root / "worker.py").write_text("old", encoding="utf-8")
            for name in ("desktop_service.py", "worker.py", "warm_worker.py", "VERSION"):
                (source / name).write_text("new-" + name, encoding="utf-8")
            runtime = desktop_runtime.DesktopRuntime(root)
            statuses = iter(({"running": False}, {"running": True}))
            fake_process = mock.Mock()
            fake_process.poll.return_value = None
            fake_process.wait.return_value = 0
            with (
                mock.patch.object(runtime, "status", side_effect=lambda: next(statuses)),
                mock.patch.object(desktop_runtime, "assets", return_value=source),
                mock.patch.object(desktop_runtime, "deploy_runtime") as deploy,
                mock.patch.object(desktop_runtime.subprocess, "Popen", return_value=fake_process) as spawn,
            ):
                result = runtime.start()
            self.assertTrue(result["running"])
            deploy.assert_not_called()
            self.assertEqual((root / "worker.py").read_text(encoding="utf-8"), "old")
            script = Path(spawn.call_args.args[0][3])
            self.assertIn("service-runtime", str(script))
            self.assertEqual(script.read_text(encoding="utf-8"), "new-desktop_service.py")

    def test_windows_sid_ignores_chinese_username_encoding(self):
        sid = "S-1-5-21-123-456-789-1001"
        for encoding in ("gbk", "utf-8"):
            output = ('"电脑\\中文用户","' + sid + '"\r\n').encode(encoding)
            with self.subTest(encoding=encoding), mock.patch("subprocess.check_output", return_value=output) as command:
                self.assertEqual(desktop_service.windows_user_sid(), sid)
                self.assertNotIn("text", command.call_args.kwargs)
                self.assertNotIn("encoding", command.call_args.kwargs)

    def test_windows_sid_rejects_missing_or_invalid_identity(self):
        for output in (b"", b'"user","not-a-sid"\r\n', b'"S-1-5-21-1001","invalid"\r\n'):
            with self.subTest(output=output), mock.patch("subprocess.check_output", return_value=output):
                with self.assertRaises(RuntimeError):
                    desktop_service.windows_user_sid()

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
            write_json(
                root / "desktop-service.json", {"url": "https://outside.example", "key": "private", "instance": "i"}
            )
            with self.assertRaises(RuntimeError):
                desktop_runtime.DesktopRuntime(root)._control("status")


class PackagedReceiverTests(unittest.TestCase):
    @unittest.skipUnless(
        (PACKAGED_APP / "runtime" / "python.exe").exists() or (PACKAGED_APP / "runtime" / "bin" / "python3").exists(),
        "Build the portable app first",
    )
    def test_sidecar_pair_register_bind_duplicate_start_and_stop(self):
        class Broker(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                result = (
                    {"node": "portable-test"}
                    if self.path.endswith("/enroll")
                    else {"jobs": [], "projects": [{"id": "project-test", "name": "Research"}], "cancellations": []}
                )
                body = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        broker = ThreadingHTTPServer(("127.0.0.1", 0), Broker)
        threading.Thread(target=broker.serve_forever, daemon=True).start()
        self.addCleanup(broker.server_close)
        self.addCleanup(broker.shutdown)
        bundle = PACKAGED_APP
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                desktop_runtime,
                "python_command",
                return_value=str(runtime_python(bundle)),
            ),
            mock.patch.object(desktop_runtime, "assets", return_value=bundle / "client"),
        ):
            root = Path(directory)
            runtime = desktop_runtime.DesktopRuntime(root / "中文用户数据")
            runtime.pair(f"http://127.0.0.1:{broker.server_address[1]}", "synthetic-one-time-code")
            with self.assertRaises(ValueError):
                runtime.pair("http://127.0.0.1:1", "again")
            try:
                self.assertTrue(runtime.start()["running"])
                instance = json.loads((runtime.root / "desktop-service.json").read_text(encoding="utf-8"))["instance"]
                runtime.start()
                self.assertEqual(
                    instance,
                    json.loads((runtime.root / "desktop-service.json").read_text(encoding="utf-8"))["instance"],
                )
                workdir = root / "project"
                workdir.mkdir()
                runtime.register_project("local-test", str(workdir), "owner-model", "high")
                runtime.save_binding(
                    {
                        "project_id": "project-test",
                        "backend_project": "local-test",
                        "name": "Test",
                        "model": "owner-model",
                        "effort": "high",
                        "timeout": 120,
                        "enabled": False,
                        "stages": ["discuss"],
                    }
                )
                state = runtime.fetch_state()
                self.assertFalse(state["bindings"][0]["enabled"])
                self.assertEqual(state["bindings"][0]["model"], "owner-model")
                self.assertNotIn("device_token", json.dumps(state))
            finally:
                runtime.stop_and_wait(timeout=20)
                self.assertFalse(runtime.status()["running"])


if __name__ == "__main__":
    unittest.main()
