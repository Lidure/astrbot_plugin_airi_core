import tempfile
import unittest
from pathlib import Path

from PIL import Image

from rank_avatars import paste_circular_avatar


class RankAvatarRenderTests(unittest.TestCase):
    def test_pastes_real_avatar_as_circle(self):
        with tempfile.TemporaryDirectory() as tmp:
            avatar_path = Path(tmp) / "avatar.png"
            Image.new("RGB", (80, 40), (250, 10, 20)).save(avatar_path)
            canvas = Image.new("RGB", (120, 120), "white")
            used_real = paste_circular_avatar(canvas, avatar_path, (20, 20, 70, 70))
            self.assertTrue(used_real)
            self.assertEqual(canvas.getpixel((45, 45)), (250, 10, 20))
            self.assertEqual(canvas.getpixel((20, 20)), (255, 255, 255))

    def test_missing_avatar_draws_placeholder(self):
        canvas = Image.new("RGB", (120, 120), "white")
        used_real = paste_circular_avatar(canvas, None, (20, 20, 70, 70))
        self.assertFalse(used_real)
        self.assertNotEqual(canvas.getpixel((45, 45)), (255, 255, 255))
        self.assertEqual(canvas.getpixel((20, 20)), (255, 255, 255))

    def test_broken_avatar_draws_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.png"
            broken.write_bytes(b"broken")
            canvas = Image.new("RGB", (120, 120), "white")
            used_real = paste_circular_avatar(canvas, broken, (20, 20, 70, 70))
            self.assertFalse(used_real)
            self.assertNotEqual(canvas.getpixel((45, 45)), (255, 255, 255))


if __name__ == "__main__":
    unittest.main()
