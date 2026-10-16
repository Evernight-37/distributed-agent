import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from http.server import ThreadingHTTPServer
from panel import handler_for


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        handler = handler_for(Path(self.tmp.name))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, csrf=None, origin=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
            headers["Origin"] = origin or f"http://127.0.0.1:{self.port}"
            headers["X-CSRF-Token"] = csrf if csrf is not None else (self.csrf or "")
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        return response.status, json.loads(payload.decode())

    def bootstrap(self):
        status, session = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.csrf = session["csrf"]
        body = json.dumps({"action": "bootstrap", "username": "owner", "password": "1"}).encode()
        status, result = self.request("POST", "/api/action", body)
        self.assertEqual(status, 200)
        self.csrf = result["csrf"]
        return result

    def test_bootstrap_login_session_and_anonymous_denial(self):
        status, _ = self.request("GET", "/api/snapshot")
        self.assertEqual(status, 401)
        result = self.bootstrap()
        self.assertEqual(result["user"]["username"], "owner")
        status, snap = self.request("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(snap["user"]["username"], "owner")

    def test_csrf_and_origin_rejected(self):
        self.request("GET", "/api/session")
        body = json.dumps({"action": "login", "username": "nobody", "password": "bad-password-12"}).encode()
        status, _ = self.request("POST", "/api/action", body, csrf="wrong")
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/action", body, csrf=self.csrf, origin="https://evil.example")
        self.assertEqual(status, 403)

    def test_nonmember_project_is_denied(self):
        self.bootstrap()
        # A project identifier must still pass server-side membership checks.
        status, _ = self.request("GET", "/api/project?id=project-does-not-exist")
        self.assertEqual(status, 403)

    def test_verified_tailscale_host_and_loopback_are_both_allowed(self):
        # The production hostname must have exactly the Tailscale ts.net shape.
        public = "https://host.tailnet.ts.net"
        (Path(self.tmp.name) / "team-service.json").write_text(
            json.dumps(
                {"url": public, "proxy": "http://127.0.0.1:8765", "mode": "tailscale-serve", "tailnet_only": True}
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
        for host, expected in ((f"127.0.0.1:{self.port}", 200), ("host.tailnet.ts.net", 200), ("evil.example", 403)):
            conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
            conn.request("GET", "/api/session", headers={"Host": host})
            response = conn.getresponse()
            response.read()
            conn.close()
            self.assertEqual(response.status, expected)


if __name__ == "__main__":
    unittest.main()
