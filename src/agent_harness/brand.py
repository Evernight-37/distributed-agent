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
