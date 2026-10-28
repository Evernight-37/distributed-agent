"""Independent desktop event waits that never occupy the WebView action lock."""

from __future__ import annotations

import copy
import json
import threading
import time


class DesktopEventPump:
    """Maintain one bounded wait loop per collaboration service."""

    def __init__(self, wait, notify):
        self.wait = wait
        self.notify = notify
        self._lock = threading.Lock()
        self._generation = 0
        self._signature = None
        self._threads = []

    def update(self, identity, servers):
        normalized = tuple(dict.fromkeys(servers or ())) if identity else ()
        signature = (
            identity.get("account_id") if identity else None,
            identity.get("public_key") if identity else None,
            normalized,
        )
        with self._lock:
            if signature == self._signature:
                return
            self._signature = signature
            self._generation += 1
            generation = self._generation
            self._threads = []
            if not identity:
                return
            snapshot = copy.deepcopy(identity)
            for server in normalized:
                thread = threading.Thread(
                    target=self._run,
                    args=(generation, snapshot, server),
                    name="harness-event-wait",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def stop(self):
        self.update(None, ())

    def _current(self, generation):
        with self._lock:
            return generation == self._generation

    def _run(self, generation, identity, server):
        cursor = 0
        failures = 0
        while self._current(generation):
            try:
                result = self.wait(identity, server, cursor, 10)
                failures = 0
                if not self._current(generation):
                    return
                next_cursor = result.get("cursor")
                if isinstance(next_cursor, int) and not isinstance(next_cursor, bool) and next_cursor >= cursor:
                    cursor = next_cursor
                if result.get("changed"):
                    self.notify(cursor, server)
            except (OSError, RuntimeError, ValueError, PermissionError):
                failures += 1
                # Old Hubs and transient network failures use the UI's low-rate
                # compatibility refresh. Avoid a hot retry loop.
                deadline = time.monotonic() + min(30, 2 ** min(failures, 4))
                while self._current(generation) and time.monotonic() < deadline:
                    time.sleep(0.1)


def notify_webview(window, cursor, server):
    """Call a fixed browser hook; JSON encoding prevents script injection."""

    if not window:
        return
    script = "window.__harnessEvent?.(%s,%s)" % (
        json.dumps(cursor),
        json.dumps(server),
    )
    try:
        window.evaluate_js(script)
    except Exception:
        # A closing or not-yet-loaded WebView is an expected race. The next
        # visibility/fallback refresh reconciles state.
        return
