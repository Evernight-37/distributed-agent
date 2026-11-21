"""The brand mark must stay one picture: tray, executable and installer icons."""

import tempfile
import unittest
from pathlib import Path

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow ships with the desktop build only
    Image = None


@unittest.skipIf(Image is None, "Pillow is not installed in this environment")
class BrandTests(unittest.TestCase):
    def test_the_mark_is_a_square_rgba_image(self):
        from agent_harness import brand

        image = brand.draw(64)
        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        # Corners stay transparent: the mark is a disc, not a tile.
        self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_the_tray_uses_the_same_drawing(self):
        from agent_harness import brand

        self.assertEqual(brand.tray_image().size, (64, 64))

    def test_written_icons_carry_every_windows_size(self):
        from agent_harness import brand

        with tempfile.TemporaryDirectory() as folder:
            made = brand.write_icons(folder)
            self.assertTrue(made["ico"].is_file())
            sizes = {size for size in Image.open(made["ico"]).info.get("sizes", [])}
            self.assertTrue({(16, 16), (32, 32), (256, 256)} <= sizes, sizes)
            self.assertEqual(Image.open(made["png"]).size, (512, 512))
            self.assertEqual(made["png"].parent, Path(folder))
