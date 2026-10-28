import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from agent_harness.panel import handler_for
from agent_harness.app_bridge import AppBridge
from agent_harness.collaboration_store import Store
from agent_harness.pull_worker import enroll, Connector
from agent_harness.owner_worker import OwnerControl
from agent_harness.common import read_json, write_json


class ProductionFlow(unittest.TestCase):
    def test_default_local_state_uses_unversioned_data_namespace(self):
        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict("os.environ", {"LOCALAPPDATA": td}),
        ):
            bridge = AppBridge()
            self.assertEqual(bridge.root, (Path(td) / "AgentHarness" / "data").resolve())

    def test_local_login_restarts_local_service_after_complete_exit(self):
        from agent_harness.account_identity import generate_identity, encrypt_identity
        from agent_harness.desktop_runtime import DesktopRuntime

        with tempfile.TemporaryDirectory() as td:
            bridge = AppBridge(Path(td))
            profile = {
                "id": "saved-local",
                "username": "本机用户",
                "server": "http://127.0.0.1:8765",
                "encrypted": encrypt_identity(generate_identity(), "password"),
            }
            with (
                patch.object(bridge, "_profiles", return_value=[profile]),
                patch.object(bridge, "_active_host_state", return_value=None),
                patch.object(
                    DesktopRuntime,
                    "start_workbench",
                    return_value="http://127.0.0.1:8765",
                ) as start,
                patch.object(bridge, "_rpc", return_value={}),
                patch.object(bridge, "_enroll"),
            ):
                result = bridge.invoke("login", {"profile": "saved-local", "password": "password"})
            self.assertTrue(result["ok"], result)
            start.assert_called_once_with(open_browser=False, state_root=bridge._host_state_root())

    def test_local_login_uses_saved_host_state_before_starting_service(self):
        from agent_harness.account_identity import generate_identity, encrypt_identity
        from agent_harness.desktop_runtime import DesktopRuntime

        with tempfile.TemporaryDirectory() as td:
            bridge = AppBridge(Path(td) / "app")
            saved = Path(td) / "old-install" / "data"
            profile = {
                "id": "saved-local",
                "username": "本机用户",
                "server": "http://127.0.0.1:8765",
                "local_host_state": str(saved),
                "encrypted": encrypt_identity(generate_identity(), "password"),
            }
            with (
                patch.object(bridge, "_profiles", return_value=[profile]),
                patch.object(bridge, "_active_host_state", return_value=None),
                patch.object(
                    DesktopRuntime,
                    "start_workbench",
                    return_value="http://127.0.0.1:8765",
                ) as start,
                patch.object(bridge, "_rpc", return_value={}),
                patch.object(bridge, "_enroll"),
            ):
                result = bridge.invoke("login", {"profile": "saved-local", "password": "password"})
            self.assertTrue(result["ok"], result)
            start.assert_called_once_with(open_browser=False, state_root=saved.resolve())

    def test_local_login_replaces_only_verified_wrong_managed_host(self):
        from agent_harness.account_identity import generate_identity, encrypt_identity
        from agent_harness.desktop_runtime import DesktopRuntime
        from agent_harness.app_lifecycle import HostRuntime

        with tempfile.TemporaryDirectory() as td:
            bridge = AppBridge(Path(td) / "app")
            saved = Path(td) / "old" / "data"
            wrong = Path(td) / "new" / "data"
            profile = {
                "id": "saved-local",
                "username": "本机用户",
                "server": "http://127.0.0.1:8765",
                "local_host_state": str(saved),
                "encrypted": encrypt_identity(generate_identity(), "password"),
            }
            with (
                patch.object(bridge, "_profiles", return_value=[profile]),
                patch.object(bridge, "_active_host_state", return_value=wrong.resolve()),
                patch.object(HostRuntime, "stop_and_wait") as stop,
                patch.object(
                    DesktopRuntime,
                    "start_workbench",
                    return_value="http://127.0.0.1:8765",
                ) as start,
                patch.object(bridge, "_rpc", return_value={}),
                patch.object(bridge, "_enroll"),
            ):
                result = bridge.invoke("login", {"profile": "saved-local", "password": "password"})
            self.assertTrue(result["ok"], result)
            stop.assert_called_once()
            start.assert_called_once_with(open_browser=False, state_root=saved.resolve())

    def test_execution_approval_recovers_service_after_app_restart(self):
        class Runtime:
            def __init__(self, root):
                self.root = root
                self.calls = []

            def _control(self, operation, payload):
                self.calls.append((operation, payload))
                return {"approved": True}

        with tempfile.TemporaryDirectory() as td:
            bridge = AppBridge(Path(td) / "app")
            bridge.identity = {"account_id": "account"}
            bridge.profile = {
                "server": "https://primary.example",
                "node": "device-test",
            }
            runtime = Runtime(Path(td) / "receiver")
            job_id = "job-restart-approval"
            receipt = runtime.root / "pull-receipts" / (hashlib.sha256(job_id.encode()).hexdigest() + ".json")
            write_json(receipt, {"phase": "waiting_local_approval", "job": {"id": job_id}})
            with (
                patch.object(bridge, "_attached_services", return_value=["https://team.example"]),
                patch.object(bridge, "_runtime", return_value=runtime),
            ):
                result = bridge.invoke("approve-execution", {"job_id": job_id, "approved": True})
            self.assertTrue(result["ok"], result)
            self.assertEqual(bridge.job_servers[job_id], "https://team.example")
            self.assertEqual(
                runtime.calls,
                [
                    (
                        "action",
                        {
                            "action": "approve-execution",
                            "job_id": job_id,
                            "approved": True,
                        },
                    )
                ],
            )

    def test_execution_approval_never_falls_back_to_primary_service(self):
        class Runtime:
            def __init__(self, root):
                self.root = root
                self.calls = []

            def _control(self, operation, payload):
                self.calls.append((operation, payload))
                return {"approved": True}

        with tempfile.TemporaryDirectory() as td:
            bridge = AppBridge(Path(td) / "app")
            bridge.identity = {"account_id": "account"}
            bridge.profile = {
                "server": "https://primary.example",
                "node": "device-test",
            }
            runtime = Runtime(Path(td) / "receiver")
            with (
                patch.object(bridge, "_attached_services", return_value=["https://team.example"]),
                patch.object(bridge, "_runtime", return_value=runtime),
            ):
                result = bridge.invoke("approve-execution", {"job_id": "job-missing", "approved": True})
            self.assertFalse(result["ok"])
            self.assertIn("找不到等待批准", result["error"])
            self.assertEqual(runtime.calls, [])

    def test_two_devices_and_owner_controls(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            handler = handler_for(root / "service")
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = "http://127.0.0.1:" + str(server.server_port)

            class Bridge(AppBridge):
                def _enroll(self):
                    runtime = self._runtime()
                    if not runtime.config_path.exists():
                        pair = self._rpc("pair-local", {"node": self.profile["node"]})
                        enroll(
                            self.profile["server"],
                            pair.get("code", pair.get("pair_code")),
                            runtime.root,
                        )
                        config = read_json(runtime.config_path)
                        config["owner_account_id"] = self.identity["account_id"]
                        write_json(runtime.config_path, config)

            a = Bridge(root / "a")
            b = Bridge(root / "b")

            def call(client, op, data=None):
                result = client.invoke(op, data)
                self.assertTrue(result["ok"], result)
                return result["result"]

            try:
                call(
                    a,
                    "register",
                    {"username": "测试账户", "password": "", "server": url},
                )
                Store(root / "service").claim_local_host(a.identity["account_id"], a.profile["node"])
                dash = call(a, "dashboard")
                self.assertEqual(len(dash["devices"]), 1)
                req = call(
                    b,
                    "device-request",
                    {"name": "第二台", "password": "短", "server": url},
                )
                call(a, "device-inspect", {"code": req["code"]})
                call(a, "device-approve", {"request_id": req["request_id"]})
                call(b, "device-claim", {"request_id": req["request_id"]})
                self.assertEqual(a.identity["account_id"], b.identity["account_id"])
                self.assertNotEqual(a.identity["private_key"], b.identity["private_key"])
                project = call(a, "new-project", {"name": "独立测试项目"})
                pid = project.get("id", project.get("project", {}).get("id"))
                call(
                    a,
                    "employee-create",
                    {"name": "第二台员工", "node": b.profile["node"]},
                )
                connector = Connector(b._runtime().config_path)
                connector.cycle()
                employees = call(a, "dashboard")["employees"]
                self.assertEqual(len(employees), 1)
                employee = employees[0]["id"]
                work = root / "project"
                work.mkdir()
                call(
                    a,
                    "employee-configure",
                    {
                        "id": employee,
                        "name": "第二台员工",
                        "model": "gpt-5.6-sol",
                        "effort": "medium",
                        "enabled": False,
                        "stages": ["discuss"],
                        "timeout": 600,
                    },
                )
                connector.cycle()
                OwnerControl(connector)._configure_workspace({"project_id": pid, "path": str(work)})
                b._rpc("workspace-ready", {"project": pid})
                workspace_view = call(a, "project", {"id": pid})["workspaces"]
                self.assertEqual(workspace_view[0]["status"], "ready")
                self.assertNotIn("path", workspace_view[0])
                self.assertNotIn("remote_path", workspace_view[0])
                call(a, "employee-dispatch", {"id": employee, "project": pid})
                connector.cycle()
                connector.cycle()
                view = call(a, "project", {"id": pid, "visit": True})
                self.assertEqual(view["members"][0]["employee_id"], employee)
                call(b, "message", {"project": pid, "body": "跨设备消息"})
                self.assertEqual(
                    call(a, "project", {"id": pid})["messages"][-1]["body"],
                    "跨设备消息",
                )
                call(a, "employee-withdraw", {"id": employee, "project": pid})
                connector.cycle()
                connector.cycle()
                self.assertEqual(call(a, "project", {"id": pid})["members"], [])
                call(
                    a,
                    "employee-configure",
                    {
                        "id": employee,
                        "name": "第二台员工",
                        "model": "gpt-5.6-sol",
                        "effort": "medium",
                        "enabled": True,
                        "stages": ["discuss"],
                        "timeout": 600,
                    },
                )
                connector.cycle()
                connector.cycle()
                self.assertFalse(connector.bindings()[0]["enabled"])
                with self.assertRaisesRegex(ValueError, "existing absolute directory"):
                    OwnerControl(connector)._configure_workspace({"project_id": pid, "path": str(root / "missing")})
                call(a, "device-revoke", {"id": b.profile["node"]})
                self.assertFalse(b.invoke("dashboard")["ok"])
                with self.assertRaises(Exception):
                    connector.cycle()
                profile = a.profile["id"]
                call(a, "logout")
                call(a, "login", {"profile": profile, "password": ""})
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
