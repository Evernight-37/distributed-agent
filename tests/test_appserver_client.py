import os
import subprocess
import sys
import unittest

from agent_harness.appserver_client import AppServer


class AppServerTests(unittest.TestCase):
    def test_bidirectional_ids_and_deferred_notifications(self):
        peer = """
import json, sys
def read():
    return json.loads(sys.stdin.readline())
def send(message):
    print(json.dumps(message), flush=True)

init = read()
send({"id": init["id"], "result": {}})
assert read()["method"] == "initialized"
for method, response in [("turn/start", {"result": {"turn": {"id": "t"}}}),
                         ("turn/interrupt", {"error": {"message": "interrupt failed"}})]:
    request = read()
    assert request["method"] == method
    send({"method": "before"})
    send({"id": request["id"], "method": "item/tool/requestUserInput", "params": {}})
    reply = read()
    assert reply["id"] == request["id"] and reply["error"]["code"] == -32601
    send({"method": "after"})
    send({"id": request["id"], **response})
"""
        server = AppServer([sys.executable, "-u", "-c", peer], None, os.environ, subprocess.DEVNULL)
        try:
            self.assertEqual(server.call("turn/start", {}, 3), {"turn": {"id": "t"}})
            with self.assertRaisesRegex(RuntimeError, "interrupt failed"):
                server.call("turn/interrupt", {}, 3)
            self.assertEqual([server.event(1)["method"] for _ in range(4)], ["before", "after", "before", "after"])
        finally:
            server.close()
            server.process.stdout.close()
