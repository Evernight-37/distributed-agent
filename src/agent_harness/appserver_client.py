"""Local Codex app-server transport and a conservative public-reply decoder.

Only the JSON ``reply`` member may be previewed.  Other structured fields may
contain private messages or machine-owner actions and must never be streamed.
"""

import json
import os
import queue
import re
import subprocess
import threading
import time

from release_info import version as application_version


class PublicReplyDecoder:
    """Extract a growing first ``reply`` string from structured JSON deltas.

    If the model chooses another key order, no preview is emitted.  The final
    structured report is still handled normally after the turn completes.
    """

    _prefix = re.compile(r'^\s*\{\s*"reply"\s*:\s*"', re.DOTALL)

    def __init__(self):
        self.raw = ""

    def feed(self, delta):
        if len(self.raw) > 2_000_000:
            return ""
        self.raw += delta
        match = self._prefix.match(self.raw)
        if not match:
            return ""
        source = self.raw[match.end() :]
        index = 0
        safe_end = 0
        while index < len(source):
            ch = source[index]
            if ch == '"':
                break
            if ch == "\\":
                if index + 1 >= len(source):
                    break
                escaped = source[index + 1]
                if escaped == "u":
                    if index + 6 > len(source) or not re.fullmatch(r"[0-9a-fA-F]{4}", source[index + 2 : index + 6]):
                        break
                    index += 6
                elif escaped in '"\\/bfnrt':
                    index += 2
                else:
                    break
            else:
                if ord(ch) < 32:
                    break
                index += 1
            safe_end = index
        try:
            value = json.loads('"' + source[:safe_end] + '"')
            # Defer a split Unicode surrogate pair until its second half arrives.
            return value.encode("utf-16", "surrogatepass").decode("utf-16", "ignore")[:16000]
        except (ValueError, UnicodeError):
            return ""


class AppServer:
    """One private stdio app-server process owned by a local worker manager."""

    def __init__(self, command, cwd, env, stderr):
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.inbox = queue.Queue()
        self.pending = []
        self.next_id = 1
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "distributed_agent_harness",
                    "title": "Distributed Agent Harness",
                    "version": application_version(),
                }
            },
            20,
        )
        self.send({"method": "initialized", "params": {}})

    def _read(self):
        for line in self.process.stdout:
            try:
                self.inbox.put(json.loads(line))
            except ValueError:
                continue
        self.inbox.put(None)

    def send(self, message):
        if self.process.poll() is not None:
            raise RuntimeError("Codex app-server exited")
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def event(self, timeout):
        if self.pending:
            return self.pending.pop(0)
        try:
            item = self.inbox.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("Codex app-server did not respond") from None
        if item is None:
            raise RuntimeError("Codex app-server disconnected")
        return item

    def call(self, method, params, timeout=30):
        request_id = self.next_id
        self.next_id += 1
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        deferred = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Codex app-server request timed out: " + method)
                try:
                    item = self.event(min(remaining, 1))
                except TimeoutError:
                    continue
                if item.get("id") == request_id:
                    if "error" in item:
                        raise RuntimeError(
                            "Codex app-server rejected " + method + ": " + str(item["error"].get("message", "error"))
                        )
                    return item.get("result", {})
                deferred.append(item)
        finally:
            self.pending = deferred + self.pending

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
