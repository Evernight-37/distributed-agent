"""The Foolship mark, drawn once and reused everywhere.

The tray icon, the executable icon and the installer icon must be the same
picture: they are the only brand surface the product has. Drawing at 8x and
downsampling keeps the curved edge and the hull's slanted sides from staircasing
at small sizes.
"""

DARK = (28, 36, 48, 255)
LIGHT = (238, 238, 238, 255)
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def draw(size=256):
    """Render the mark at ``size`` pixels, square and transparent outside the disc."""
    from PIL import Image, ImageDraw

    oversample = 8
    canvas = size * oversample
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = canvas / 64.0

    def point(x, y):
        return (x * scale, y * scale)

    draw.ellipse([point(2, 2), point(62, 62)], fill=DARK)
    # A sail over a hull, the same geometry the tray icon used to hardcode.
    draw.polygon([point(32, 12), point(46, 37), point(32, 37)], fill=LIGHT)
    draw.polygon([point(14, 43), point(50, 43), point(43, 52), point(21, 52)], fill=LIGHT)
    return image.resize((size, size), Image.LANCZOS)


def tray_image():
    """The tray asks for a handful of pixels: 64 is plenty on every DPI."""
    return draw(64)


def write_icons(directory):
    """Write ``foolship.ico``, ``foolship.png`` and (best effort) ``.icns``.

    Returns the paths that were produced. Nothing here is committed: the build
    scripts generate the icons from this module, so the picture cannot drift from
    the code that draws it.
    """
    from pathlib import Path

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    produced = {}
    ico = directory / "foolship.ico"
    draw(256).save(ico, format="ICO", sizes=[(size, size) for size in SIZES])
    produced["ico"] = ico
    png = directory / "foolship.png"
    draw(512).save(png, format="PNG")
    produced["png"] = png
    icns = directory / "foolship.icns"
    try:
        draw(1024).save(icns, format="ICNS")
    except Exception:
        icns.unlink(missing_ok=True)
    else:
        produced["icns"] = icns
    return produced


def window_icon(root):
    """An .ico on disk for the native window, or None when Pillow is missing.

    pywebview's WinForms backend extracts the icon from the executable when none
    is given, and that fallback can leave the default form icon in the taskbar.
    Writing the mark once per install makes the window icon explicit.
    """
    from pathlib import Path

    try:
        target = Path(root) / "window-icon.ico"
        icon = draw(256)
        if not target.is_file():
            icon.save(target, format="ICO", sizes=[(size, size) for size in SIZES])
        return str(target)
    except Exception:
        return None


def refresh_shell_icons(root):
    """Ask Explorer to rebuild its cached icon once per version.

    Upgrades replace Foolship.exe in place, which no installer would do, so
    Explorer keeps showing the previous (default) icon on shortcuts and in the
    taskbar until its cache is rebuilt. The marker makes this a one-time cost per
    version instead of a shell refresh on every launch.
    """
    import sys

    if sys.platform != "win32":
        return False
    from pathlib import Path

    from agent_harness.release_info import version

    try:
        marker = Path(root) / "icon-cache.version"
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == version():
            return False
        import ctypes

        # SHCNE_ASSOCCHANGED with SHCNF_IDLIST: the documented way to ask the
        # shell to rebuild icons after an application changed.
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
        marker.write_text(version() + "\n", encoding="utf-8")
        return True
    except Exception:
        return False
