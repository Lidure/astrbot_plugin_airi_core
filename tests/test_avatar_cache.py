import io
import os
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from avatar_cache import QqAvatarCache, qq_avatar_url


def png_bytes(color=(220, 80, 120, 255), size=(32, 32)):
    image = Image.new("RGBA", size, color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class AvatarCacheTests(unittest.TestCase):
    def test_qq_avatar_url_uses_public_qlogo_endpoint(self):
        self.assertEqual(
            qq_avatar_url("123456"),
            "https://q1.qlogo.cn/g?b=qq&nk=123456&s=100",
        )

    def test_downloads_and_normalizes_avatar_to_png(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            cache = QqAvatarCache(
                tmp, fetcher=lambda url: calls.append(url) or png_bytes()
            )
            result = cache.get("123456")
            self.assertIsNotNone(result)
            path = Path(result)
            self.assertTrue(path.is_file())
            self.assertEqual(path.suffix, ".png")
            with Image.open(path) as image:
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.size, (32, 32))
            self.assertEqual(len(calls), 1)

    def test_fresh_cache_avoids_second_download(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            cache = QqAvatarCache(
                tmp, fetcher=lambda url: calls.append(url) or png_bytes()
            )
            first = cache.get("123456")
            second = cache.get("123456")
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)

    def test_expired_cache_refreshes(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            cache = QqAvatarCache(
                tmp,
                ttl_seconds=1,
                fetcher=lambda url: calls.append(url) or png_bytes(),
            )
            result = Path(cache.get("123456"))
            old = time.time() - 20
            os.utime(result, (old, old))
            cache.get("123456")
            self.assertEqual(len(calls), 2)

    def test_download_failure_uses_stale_cached_avatar(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = QqAvatarCache(tmp, ttl_seconds=1, fetcher=lambda url: png_bytes())
            result = Path(cache.get("123456"))
            old = time.time() - 20
            os.utime(result, (old, old))
            cache.fetcher = lambda url: (_ for _ in ()).throw(OSError("offline"))
            self.assertEqual(cache.get("123456"), str(result))

    def test_invalid_user_id_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = QqAvatarCache(tmp, fetcher=lambda url: png_bytes())
            self.assertIsNone(cache.get("abc"))
            self.assertIsNone(cache.get(""))

    def test_invalid_download_does_not_replace_valid_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = QqAvatarCache(tmp, ttl_seconds=1, fetcher=lambda url: png_bytes())
            result = Path(cache.get("123456"))
            old = time.time() - 20
            os.utime(result, (old, old))
            cache.fetcher = lambda url: b"not-an-image"
            self.assertEqual(cache.get("123456"), str(result))
            with Image.open(result) as image:
                self.assertEqual(image.size, (32, 32))


if __name__ == "__main__":
    unittest.main()
