"""What a person sees when the collaboration service answers with a page instead of JSON."""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_harness.app_bridge import AppBridge


class PageHandler(BaseHTTPRequestHandler):
    status = 200
    body = b"<html><body>bad gateway</body></html>"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(self.status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        return


class RpcErrorTextTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.bridge = AppBridge.__new__(AppBridge)
        self.bridge.broker_proxy = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        PageHandler.status = 200

    def test_a_page_instead_of_json_reads_as_a_retryable_message(self):
        with self.assertRaises(RuntimeError) as caught:
            self.bridge._post(self.url, {"action": "challenge"})
        message = str(caught.exception)
        self.assertIn("无法解析的响应", message)
        # The decoder's own wording is not something anyone can act on.
        self.assertNotIn("Expecting value", message)

    def test_an_error_page_carries_its_status(self):
        PageHandler.status = 502
        with self.assertRaises(RuntimeError) as caught:
            self.bridge._post(self.url, {"action": "challenge"})
        self.assertIn("502", str(caught.exception))
