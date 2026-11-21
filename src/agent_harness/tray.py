"""System tray icon for the Windows desktop shell.

The window can stay closed while the receiver keeps working, so the tray is the
only way back to it. Where pystray is unavailable (no dependency, or a platform
without a tray), the shell falls back to its previous behaviour.
"""

from __future__ import annotations

import sys
import threading


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
    """A tray icon with "show" and "quit" actions."""

    def __init__(self, title, show, quit_app):
        import pystray

        self._icon = pystray.Icon(
            "foolship",
            _image(),
            title,
            menu=pystray.Menu(
                pystray.MenuItem("显示窗口", lambda *_: show(), default=True),
                pystray.MenuItem("完全退出", lambda *_: quit_app()),
            ),
        )
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._icon.run, name="tray", daemon=True)
        self._thread.start()

    def stop(self):
        try:
            self._icon.stop()
        except Exception:
            pass
