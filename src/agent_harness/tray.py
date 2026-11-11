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
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(28, 36, 48, 255))
    # A sail over a hull: the mark the window uses for the ship, at tray size.
    draw.polygon([(32, 12), (46, 37), (32, 37)], fill=(238, 238, 238, 255))
    draw.polygon([(14, 43), (50, 43), (43, 52), (21, 52)], fill=(238, 238, 238, 255))
    return image


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
