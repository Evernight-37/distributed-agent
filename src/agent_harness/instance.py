"""One desktop process per data directory, and a way to raise the running one.

Starting the app twice used to produce two independent windows that fought over
the same ports and data. The first process keeps a loopback listener and a small
record of it; a later start asks that listener to show its window and exits.
"""

from __future__ import annotations

import json
import secrets
import socket
import threading
from pathlib import Path


class Instance:
    """The running desktop process for one data directory."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "instance.json"
        self.token = secrets.token_hex(16)
        self._socket = None
        self._thread = None

    def claim(self, on_show=None):
        """Own the application, or ask the owner to come forward.

        Returns True when this process is the only one, False when another
        instance was running and has been asked to show its window.
        """
        if self._raise_existing():
            return False
        self._listen(on_show)
        self._write()
        return True

    def release(self):
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        try:
            recorded = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if recorded.get("token") == self.token:
            try:
                self.path.unlink()
            except OSError:
                pass

    def _write(self):
        payload = {"port": self._socket.getsockname()[1], "token": self.token}
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload) + "\n", "utf-8")

    def _raise_existing(self):
        try:
            recorded = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return False
        port = recorded.get("port") if isinstance(recorded, dict) else None
        token = recorded.get("token") if isinstance(recorded, dict) else None
        if not isinstance(port, int) or not isinstance(token, str):
            self._forget()
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.6) as client:
                client.sendall(json.dumps({"action": "show", "token": token}).encode("utf-8"))
                client.settimeout(0.6)
                reply = client.recv(64)
        except OSError:
            self._forget()
            return False
        return reply.strip() == b"ok"

    def _forget(self):
        try:
            self.path.unlink()
        except OSError:
            pass

    def _listen(self, on_show):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        self._socket = server

        def serve():
            while True:
                try:
                    client, _ = server.accept()
                except OSError:
                    return
                with client:
                    client.settimeout(1.0)
                    try:
                        request = json.loads(client.recv(4096).decode("utf-8"))
                    except (OSError, ValueError):
                        continue
                    if (
                        isinstance(request, dict)
                        and request.get("action") == "show"
                        and request.get("token") == self.token
                        and on_show is not None
                    ):
                        try:
                            on_show()
                        except Exception:
                            continue
                        client.sendall(b"ok")

        self._thread = threading.Thread(target=serve, name="instance-listen", daemon=True)
        self._thread.start()
