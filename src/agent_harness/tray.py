"""System tray icon for the Windows desktop shell.

The window can stay closed while the receiver keeps working, so the tray is the
only way back to it. Where pystray is unavailable (no dependency, or a platform
without a tray), the shell falls back to its previous behaviour.
"""

from __future__ import annotations

import logging
import sys
import threading

_LOG = logging.getLogger(__name__)
_NOTIFICATIONS = {
    "message": "有新的项目消息。打开 Foolship 查看。",
    "approval": "有新的审批请求。打开 Foolship 查看。",
}


def available():
    """Whether this build can show a tray icon at all."""
    if sys.platform != "win32":
        return False
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return True


def _image():
    from agent_harness import brand

    return brand.tray_image()


class Tray:
    """A Windows tray icon with a status tooltip and private notifications."""

    def __init__(self, title, show, quit_app):
        import pystray

        self._title = title
        self._icon = pystray.Icon(
            "foolship",
            _image(),
            title,
            menu=pystray.Menu(
                pystray.MenuItem("显示窗口", lambda *_: show(), default=True),
                pystray.MenuItem("完全退出", lambda *_: quit_app()),
            ),
        )
        self._lock = threading.Lock()
        self._initialized = threading.Event()
        self._ready = False
        self._stopping = False
        self._thread = None

    @property
    def ready(self):
        with self._lock:
            return self._ready

    def start(self, timeout=2):
        """Wait briefly for the native icon before relying on it to restore a window."""
        with self._lock:
            if self._stopping:
                return False
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
                self._thread.start()
        self._initialized.wait(timeout)
        return self.ready

    def _run(self):
        try:
            self._icon.run(setup=self._initialize_icon)
        except Exception:
            _LOG.exception("Foolship tray could not run")
        finally:
            with self._lock:
                self._ready = False
                self._initialized.set()

    def _initialize_icon(self, icon):
        with self._lock:
            try:
                if not self._stopping:
                    icon.visible = True
                    self._ready = icon.visible
            except Exception:
                _LOG.exception("Foolship tray icon could not be shown")
            self._initialized.set()
            keep_running = self._ready
        if not keep_running:
            self._stop_icon()

    def set_status(self, status):
        """Set the tooltip. Windows reserves 127 UTF-16 code units for its contents."""
        title = f"{self._title} · {status}" if status else self._title
        bounded = title.encode("utf-16-le")[:254].decode("utf-16-le", errors="ignore")
        with self._lock:
            if self._stopping:
                return False
            try:
                self._icon.title = bounded
            except Exception:
                _LOG.exception("Foolship tray tooltip could not be updated")
                return False
            return self._ready

    def notify(self, kind="message"):
        """Request a generic Windows notification without any account or message details."""
        message = _NOTIFICATIONS[kind]
        with self._lock:
            if not self._ready or not self._icon.HAS_NOTIFICATION:
                return False
            try:
                self._icon.notify(message, self._title)
            except Exception:
                _LOG.exception("Foolship tray notification could not be submitted")
                return False
            return True

    def stop(self):
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            was_ready = self._ready
            self._ready = False
            self._initialized.set()
        if was_ready:
            self._stop_icon()
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=1)

    def _stop_icon(self):
        try:
            self._icon.stop()
        except Exception:
            _LOG.exception("Foolship tray could not stop")
