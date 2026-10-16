import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'agent_harness'))
from mcp_server import run_stdio


class Backend:
    def tool_specs(self):
        return [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}]

    def call(self, name, arguments):
        if name == "missing":
            raise LookupError("unknown tool")
        if arguments.get("fail"):
            raise RuntimeError("business failure")
        return {"name": name, "arguments": arguments}


def run(*requests):
    inp = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
    out = io.StringIO()
    run_stdio(Backend(), inp, out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


class McpServerTests(unittest.TestCase):
    def test_initialize(self):
        result = run({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})[0]
        self.assertEqual(result["result"]["serverInfo"]["name"], "agent-harness")
        self.assertEqual(result["result"]["serverInfo"]["version"],
                         Path(__file__).resolve().parents[1].joinpath("VERSION").read_text().strip())
        self.assertIn("tools", result["result"]["capabilities"])

    def test_tools_list(self):
        result = run({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[0]
        self.assertEqual(result["result"]["tools"][0]["name"], "echo")

    def test_successful_call(self):
        result = run({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"x": 1}}})[0]
        self.assertEqual(result["result"]["structuredContent"]["arguments"], {"x": 1})
        self.assertNotIn("isError", result["result"])

    def test_business_failure(self):
        result = run({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "echo", "arguments": {"fail": True}}})[0]
        self.assertTrue(result["result"]["isError"])
        self.assertIn("business failure", result["result"]["content"][0]["text"])

    def test_unknown_tool_is_business_failure(self):
        result = run({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "missing"}})[0]
        self.assertTrue(result["result"]["isError"])

    def test_notification_has_no_response(self):
        self.assertEqual(run({"jsonrpc": "2.0", "method": "notifications/initialized"}), [])

    def test_continuous_requests_and_ping(self):
        results = run(
            {"jsonrpc": "2.0", "id": 6, "method": "ping"},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
        )
        self.assertEqual([item["id"] for item in results], [6, 7])

    def test_parse_error_does_not_stop_processing(self):
        inp = io.StringIO('{bad json\n' + json.dumps({"jsonrpc": "2.0", "id": 8, "method": "ping"}) + "\n")
        out = io.StringIO()
        run_stdio(Backend(), inp, out)
        results = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(results[0]["error"]["code"], -32700)
        self.assertEqual(results[1]["id"], 8)


if __name__ == "__main__":
    unittest.main()
