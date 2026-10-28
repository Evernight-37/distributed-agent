import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from http.server import ThreadingHTTPServer

from agent_harness.account_identity import generate_identity
from agent_harness.panel import RETIRED_BROWSER_APIS, handler_for


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        handler = handler_for(Path(self.tmp.name))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, target, value=None, host=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        body = None
        if value is not None:
            body = json.dumps(value).encode()
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, response.getheader("Content-Type"), payload)
        connection.close()
        return result

    def test_root_is_migration_page_and_old_assets_are_gone(self):
        status, content_type, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/html; charset=utf-8")
        self.assertIn("浏览器工作台已经停用".encode(), body)
        self.assertEqual(self.request("GET", "/hub/app.js")[0], 404)
        self.assertEqual(self.request("GET", "/hub/styles.css")[0], 404)

    def test_healthz_is_public_but_contains_only_service_metadata(self):
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(value["service"], "agent-harness-hub")
        self.assertIs(value["ok"], True)
        self.assertEqual(set(value), {"ok", "service", "protocol"})

    def test_legacy_cookie_browser_apis_are_gone(self):
        for endpoint in sorted(RETIRED_BROWSER_APIS - {"/api/action"}):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.request("GET", endpoint)[0], 410)
        self.assertEqual(self.request("POST", "/api/action", {"action": "login"})[0], 410)

    def test_signed_account_api_remains_available(self):
        identity = generate_identity()
        status, _, body = self.request(
            "POST",
            "/api/account",
            {"action": "challenge", "public_key": identity["public_key"]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["nonce"])

    def test_verified_tailscale_host_and_loopback_are_allowed(self):
        public = "https://host.tailnet.ts.net"
        (Path(self.tmp.name) / "team-service.json").write_text(
            json.dumps(
                {
                    "url": public,
                    "proxy": "http://127.0.0.1:8765",
                    "mode": "tailscale-serve",
                    "tailnet_only": True,
                }
            ),
            encoding="utf-8",
        )
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        handler = handler_for(Path(self.tmp.name))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        for host, expected in (
            (f"127.0.0.1:{self.port}", 200),
            ("host.tailnet.ts.net", 200),
            ("evil.example", 403),
        ):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/healthz", host=host)[0], expected)


if __name__ == "__main__":
    unittest.main()
