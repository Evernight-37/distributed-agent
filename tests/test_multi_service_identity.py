import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app_bridge import AppBridge
from collaboration_store import Store
from panel import handler_for


class QuietBridge(AppBridge):
    def _enroll(self, server=None):
        # The account and routing protocol can be tested without starting workers.
        return None


class MultiServiceIdentityTests(unittest.TestCase):
    def test_dashboard_fetches_services_concurrently_and_marks_offline(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = QuietBridge(Path(td))
            bridge.identity = {"account_id": "account", "public_key": "public"}
            bridge.profile = {
                "server": "https://one.example",
                "services": ["https://two.example", "https://bad.example"],
            }

            def activate(server, timeout=15):
                time.sleep(0.15)
                if "bad" in server:
                    raise RuntimeError("offline")

            def rpc(operation, payload=None, server=None, timeout=15):
                self.assertEqual(operation, "dashboard")
                return {
                    "service_id": server,
                    "hosted_by_me": False,
                    "projects": [],
                    "employees": [],
                    "devices": [],
                    "user": {"id": "account", "username": "owner"},
                }

            started = time.monotonic()
            with (
                patch.object(bridge, "_sync_service_links"),
                patch.object(bridge, "_activate_service", side_effect=activate),
                patch.object(bridge, "_rpc", side_effect=rpc),
            ):
                result = bridge._dashboard()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.4)
            self.assertEqual(result["unavailable_services"], ["https://bad.example"])
            self.assertEqual(len(result["services"]), 2)

    def test_one_identity_joins_other_service_and_linked_device_sees_project(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hosts = []

            def serve(name):
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(root / name))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                hosts.append((server, thread))
                return "http://127.0.0.1:" + str(server.server_port)

            def call(bridge, action, data=None):
                response = bridge.invoke(action, data)
                self.assertTrue(response["ok"], response)
                return response["result"]

            try:
                home_url, team_url = serve("home"), serve("team")
                member = QuietBridge(root / "member")
                host = QuietBridge(root / "host")
                linked = QuietBridge(root / "linked")
                call(member, "register", {"username": "Member", "password": "local", "server": home_url})
                member_account = member.identity["account_id"]
                Store(root / "home").claim_local_host(member_account, member.profile["node"])
                call(host, "register", {"username": "Host", "password": "local", "server": team_url})
                Store(root / "team").claim_local_host(host.identity["account_id"], host.profile["node"])
                project = call(host, "new-project", {"name": "Team project"})["id"]
                invite = call(host, "invite", {"project": project})["invite"]

                joined = call(member, "join-project", {"server": team_url, "invite": invite})
                self.assertEqual(joined["server"], team_url)
                dashboard = call(member, "dashboard")
                self.assertEqual(dashboard["user"]["id"], member_account)
                self.assertEqual(next(p for p in dashboard["projects"] if p["id"] == project)["server"], team_url)
                call(member, "message", {"project": project, "body": "Existing identity works"})
                self.assertEqual(
                    call(host, "project", {"id": project})["messages"][-1]["body"], "Existing identity works"
                )
                self.assertFalse(next(s for s in dashboard["services"] if s["server"] == team_url)["hosted_by_me"])
                rejected = member.invoke("new-project", {"name": "Member project", "server": team_url})
                self.assertFalse(rejected["ok"])
                self.assertIn("只能", rejected["error"])
                own_project = call(member, "new-project", {"name": "Member project", "server": home_url})
                self.assertEqual(own_project["server"], home_url)

                request = call(
                    linked, "device-request", {"name": "Second device", "password": "other", "server": home_url}
                )
                call(member, "device-inspect", {"code": request["code"]})
                call(member, "device-approve", {"request_id": request["request_id"]})
                call(linked, "device-claim", {"request_id": request["request_id"]})
                linked_dashboard = call(linked, "dashboard")
                self.assertEqual(linked.identity["account_id"], member_account)
                self.assertEqual(
                    next(p for p in linked_dashboard["projects"] if p["id"] == project)["server"], team_url
                )
                self.assertEqual(call(linked, "project", {"id": project})["project"]["id"], project)
                self.assertEqual(linked_dashboard["services"][1]["attached"], False)

                profile_id = member.profile["id"]
                call(member, "logout")
                call(member, "login", {"profile": profile_id, "password": "local"})
                self.assertEqual(call(member, "project", {"id": project})["project"]["id"], project)

                # The first service is a directory for linked devices, not a
                # required gateway for projects already joined elsewhere.
                home, home_thread = hosts.pop(0)
                home.shutdown()
                home.server_close()
                home_thread.join()
                call(member, "logout")
                call(member, "login", {"profile": profile_id, "password": "local"})
                offline_dashboard = call(member, "dashboard")
                self.assertIn(home_url, offline_dashboard["unavailable_services"])
                self.assertEqual(
                    next(p for p in offline_dashboard["projects"] if p["id"] == project)["server"], team_url
                )
            finally:
                for server, thread in hosts:
                    server.shutdown()
                    server.server_close()
                    thread.join()


if __name__ == "__main__":
    unittest.main()
