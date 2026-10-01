import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import controller  # noqa: E402
from tests import v2_panel_fixture as panel  # noqa: E402
import worker  # noqa: E402
from common import write_json  # noqa: E402


class RoutedHarness(controller.Harness):
    calls = []
    responses = {}

    def rpc(self, request):
        self.calls.append((self.node, request))
        if request.get("action") == "probe":
            return {"node": self.node, "ok": True}
        return self.responses.get(request.get("action"), {"ok": True})


class ControllerV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "nodes").mkdir()
        (self.root / "projects").mkdir()
        (self.root / "jobs").mkdir()
        (self.root / "memory").mkdir()
        (self.root / "inbox").mkdir()
        RoutedHarness.calls = []
        RoutedHarness.responses = {}

    def tearDown(self):
        self.tmp.cleanup()

    def test_rpc_routes_project_to_registered_node(self):
        write_json(self.root / "nodes" / "lab.json", {"host": "192.0.2.20", "user": "worker", "identity_file": "SECRET", "known_hosts_file": "SECRET", "remote_python": "python", "remote_root": "C:/worker"})
        write_json(self.root / "projects" / "demo.json", {"name": "demo", "node": "lab", "model": "m", "effort": "medium"})
        with mock.patch.object(controller.Harness, "transport", return_value={"node": "lab", "ok": True}):
            result = controller.Harness(self.root).rpc({"action": "probe", "project": "demo"})
        self.assertEqual(result["node"], "lab")

    def test_snapshot_excludes_ssh_secret_paths(self):
        write_json(self.root / "nodes" / "lab.json", {"host": "192.0.2.20", "user": "worker", "platform": "windows", "port": 22, "identity_file": "C:/SECRET/key", "known_hosts_file": "C:/SECRET/known"})
        write_json(self.root / "projects" / "demo.json", {"name": "demo", "node": "lab", "local_path": "C:/private/research", "remote_path": "C:/private/work", "model": "m", "effort": "medium"})
        snapshot = controller.Harness(self.root).snapshot()
        encoded = json.dumps(snapshot)
        self.assertNotIn("identity_file", encoded)
        self.assertNotIn("known_hosts_file", encoded)
        self.assertEqual(snapshot["nodes"][0]["host"], "192.0.2.20")

    def test_configure_updates_model_without_losing_node(self):
        write_json(self.root / "projects" / "demo.json", {"name": "demo", "node": "lab", "model": "old", "effort": "low", "timeout": 1})
        h = RoutedHarness(self.root, "lab")
        h.configure("demo", "new", "high", 60)
        saved = json.loads((self.root / "projects" / "demo.json").read_text())
        self.assertEqual(saved["node"], "lab")
        self.assertEqual(saved["model"], "new")
        self.assertEqual(RoutedHarness.calls[-1][0], "lab")

    def test_configure_rejected_by_active_worker_preserves_local_config(self):
        original = {"name": "demo", "node": "lab", "model": "old", "effort": "low", "timeout": 1}
        write_json(self.root / "projects" / "demo.json", original)
        h = RoutedHarness(self.root)
        h.responses = {"configure": None}
        with mock.patch.object(h, "rpc", side_effect=controller.RemoteRejected("Project already has an active job")):
            with self.assertRaises(controller.RemoteRejected):
                h.configure("demo", "new", "high", 60)
        self.assertEqual(json.loads((self.root / "projects" / "demo.json").read_text()), original)

    def test_explicit_node_mismatch_rejected_before_transport(self):
        write_json(self.root / "nodes" / "lab-a.json", {"host": "a", "user": "u", "remote_python": "python", "remote_root": "C:/worker"})
        write_json(self.root / "projects" / "demo.json", {"name": "demo", "node": "lab-b", "model": "m", "effort": "medium"})
        h = controller.Harness(self.root, "lab-a")
        with mock.patch.object(h, "transport") as transport:
            with self.assertRaisesRegex(ValueError, "differs"):
                h.rpc({"action": "probe", "project": "demo"})
        transport.assert_not_called()

    def test_pending_configuration_recovers_canonical_values_and_node(self):
        write_json(self.root / "projects" / "demo.json", {"name": "demo", "node": "lab", "model": "new", "effort": "high", "timeout": 60, "configuration_pending": True})
        h = RoutedHarness(self.root, "lab")
        with mock.patch.object(h, "rpc", return_value={"project": {"name": "demo", "node": "lab", "model": "canonical", "effort": "medium", "timeout": 1800}}) as rpc:
            recovered = h.project("demo")
        self.assertEqual(recovered["node"], "lab")
        self.assertEqual(recovered["model"], "canonical")
        self.assertNotIn("configuration_pending", recovered)
        self.assertEqual(rpc.call_args.args[0], {"action": "project", "project": "demo"})


class WorkerConfigureTests(unittest.TestCase):
    def test_configure_rejects_active_job(self):
        with tempfile.TemporaryDirectory() as directory:
            old_root = worker.ROOT
            worker.ROOT = Path(directory)
            try:
                project_dir = worker.ROOT / "projects" / "demo"
                job_dir = worker.ROOT / "jobs" / "job-1"
                project_dir.mkdir(parents=True)
                job_dir.mkdir(parents=True)
                write_json(project_dir / "project.json", {"name": "demo", "model": "old", "effort": "low", "timeout": 1, "remote_path": directory})
                write_json(project_dir / "active.json", {"job": "job-1", "pid": 999999, "stamp": "x"})
                write_json(job_dir / "state.json", {"status": "running"})
                with mock.patch.object(worker, "process_stamp", return_value="x"):
                    with self.assertRaisesRegex(ValueError, "active job"):
                        worker.rpc({"action": "configure", "project": "demo", "model": "new", "effort": "high", "timeout": 60})
                self.assertEqual(json.loads((project_dir / "project.json").read_text())["model"], "old")
            finally:
                worker.ROOT = old_root


class PanelHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "nodes").mkdir()
        self.fake = mock.patch.object(panel, "Harness", FakePanelHarness)
        self.fake.start()
        self.server = panel.ThreadedHTTPServer if hasattr(panel, "ThreadedHTTPServer") else panel.ThreadingHTTPServer
        self.server = self.server(("127.0.0.1", 0), panel.handler_for(self.root))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.fake.stop()
        self.tmp.cleanup()

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def test_rejects_invalid_host_and_origin_token(self):
        status, _ = self.request("GET", "/api/session", {"Host": "evil.test"})
        self.assertEqual(status, 403)
        status, data = self.request("GET", "/api/session", {"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        token = json.loads(data)["token"]
        headers = {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://evil:{self.port}", "X-CSRF-Token": token, "Content-Type": "application/json"}
        status, _ = self.request("POST", "/api/action", headers, json.dumps({"action": "probe", "node": "lab"}))
        self.assertEqual(status, 403)
        headers["Origin"] = f"http://127.0.0.1:{self.port}"
        headers["X-CSRF-Token"] = "wrong"
        status, _ = self.request("POST", "/api/action", headers, json.dumps({"action": "probe", "node": "lab"}))
        self.assertEqual(status, 403)

    def test_accepts_valid_session_token_for_probe(self):
        host = f"127.0.0.1:{self.port}"
        _, data = self.request("GET", "/api/session", {"Host": host})
        token = json.loads(data)["token"]
        headers = {"Host": host, "Origin": f"http://{host}", "X-CSRF-Token": token, "Content-Type": "application/json"}
        status, data = self.request("POST", "/api/action", headers, json.dumps({"action": "probe", "node": "lab"}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["ok"], True)


class FakePanelHarness:
    def __init__(self, root, node=None):
        self.node = node

    def snapshot(self):
        return {"nodes": [], "projects": [], "jobs": [], "capabilities": {}}

    def status(self, job, fetch=False):
        return {"job": job, "status": "needs_review"}

    def rpc(self, request):
        return {"ok": True, "node": self.node}


if __name__ == "__main__":
    unittest.main()
