"""Small dependency-free MCP JSON-RPC stdio adapter.

The adapter intentionally knows nothing about Harness identity or transport.  A
backend only needs ``tool_specs()`` and ``call(name, arguments)`` methods.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO


def server_version() -> str:
    """Return the release version shipped beside this MCP server."""
    try:
        value = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return value or "unknown"


class JsonRpcError(Exception):
    """An error that should be returned as a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> str:
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is None:
        response["result"] = result
    else:
        response["error"] = error
    return json.dumps(response, ensure_ascii=False, separators=(",", ":"))


def _error(request_id: Any, code: int, message: str, data: Any = None) -> str:
    details = {"code": code, "message": message}
    if data is not None:
        details["data"] = data
    return _response(request_id, error=details)


def _text_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    """Build the MCP CallToolResult shape from a JSON-compatible value."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": value,
        **({"isError": True} if is_error else {}),
    }


def _dispatch(backend: Any, method: str, params: Any) -> Any:
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-harness", "version": server_version()},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": list(backend.tool_specs())}
    if method == "tools/call":
        if not isinstance(params, dict):
            raise JsonRpcError(-32602, "params must be an object")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise JsonRpcError(-32602, "tools/call requires a non-empty name")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise JsonRpcError(-32602, "tools/call arguments must be an object")
        try:
            value = backend.call(name, arguments)
        except Exception as exc:  # business failures stay inside MCP
            return _text_result({"error": str(exc)}, is_error=True)
        return _text_result(value)
    raise JsonRpcError(-32601, f"Method not found: {method}")


def run_stdio(backend: Any, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Read newline-delimited JSON-RPC requests and write responses.

    Notifications (requests without an ``id``) are handled but produce no
    output.  Malformed input produces a standard parse error and processing
    continues, allowing a malformed line to be followed by valid requests.
    """
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    for line in source:
        if not line.strip():
            continue
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise JsonRpcError(-32600, "Invalid Request")
            request_id = request.get("id")
            method = request.get("method")
            if not isinstance(method, str):
                raise JsonRpcError(-32600, "Invalid Request")
            if "id" not in request:
                _dispatch(backend, method, request.get("params", {}))
                continue
            result = _dispatch(backend, method, request.get("params", {}))
            sink.write(_response(request_id, result=result) + "\n")
        except json.JSONDecodeError as exc:
            sink.write(_error(None, -32700, "Parse error", str(exc)) + "\n")
        except JsonRpcError as exc:
            sink.write(_error(request_id, exc.code, exc.message, exc.data) + "\n")
        except Exception as exc:
            sink.write(_error(request_id, -32603, "Internal error", str(exc)) + "\n")
        sink.flush()

