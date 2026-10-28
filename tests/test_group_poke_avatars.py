import io
import os
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from avatar_cache import QqAvatarCache, qq_avatar_url, qq_group_avatar_url
from group_rank_renderer import render_group_rank_image


def png_bytes(color=(240, 40, 80), size=(80, 80)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class GroupAvatarCacheTests(unittest.TestCase):
    def test_group_avatar_uses_qq_group_not_user_endpoint(self):
        self.assertEqual(
            qq_group_avatar_url("123456789"),
            "https://p.qlogo.cn/gh/123456789/123456789/100",
        )
        self.assertEqual(
            qq_avatar_url("123456789"),
            "https://q1.qlogo.cn/g?b=qq&nk=123456789&s=100",
        )

    def test_group_avatar_cached_independently_from_user_avatar(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            group = QqAvatarCache(
                Path(tmp) / "group_avatar_cache",
                url_builder=qq_group_avatar_url,
                fetcher=lambda url: seen.append(url) or png_bytes(),
            )
            user = QqAvatarCache(
                Path(tmp) / "avatar_cache",
                fetcher=lambda url: seen.append(url) or png_bytes((20, 100, 220)),
            )
            group_path = Path(group.get("123456789"))
            user_path = Path(user.get("123456789"))
            self.assertNotEqual(group_path, user_path)
            self.assertTrue(group_path.exists())
            self.assertTrue(user_path.exists())
            self.assertEqual(len(seen), 2)
            self.assertIn("p.qlogo.cn/gh/", seen[0])
            self.assertIn("q1.qlogo.cn/g?", seen[1])
            self.assertEqual(group.get("123456789"), str(group_path))
            self.assertEqual(len(seen), 2)

    def test_group_avatar_refresh_failure_keeps_valid_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = QqAvatarCache(
                Path(tmp) / "group_avatar_cache",
                ttl_seconds=1,
                url_builder=qq_group_avatar_url,
                fetcher=lambda url: png_bytes(),
            )
            path = Path(cache.get("123456789"))
            old = time.time() - 200
            os.utime(path, (old, old))
            cache.fetcher = lambda url: (_ for _ in ()).throw(OSError("offline"))
            self.assertEqual(cache.get("123456789"), str(path))

    def test_invalid_group_id_never_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = QqAvatarCache(
                Path(tmp) / "group_avatar_cache",
                url_builder=qq_group_avatar_url,
                fetcher=lambda url: self.fail("unexpected network request"),
            )
            self.assertIsNone(cache.get("../bad"))
            self.assertIsNone(cache.get(""))


class FakeGroupCache:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.calls = []

    def get(self, group_id):
        self.calls.append(str(group_id))
        return self.mapping.get(str(group_id))


class GroupAvatarRenderTests(unittest.TestCase):
    @staticmethod
    def summary(entries):
        return {
            "entries": entries,
            "total_groups": len(entries),
            "total_pokes": sum(c for _, c in entries),
            "display_names": {gid: f"测试群{gid}" for gid, _ in entries},
            "is_daily": True,
            "updated_at": "2026-10-28 12:00:00",
        }

    def test_real_group_avatar_is_circular_and_missing_avatar_has_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            avatar = tmp / "red.png"
            Image.new("RGB", (80, 80), (250, 15, 45)).save(avatar)
            out = tmp / "rank.png"
            cache = FakeGroupCache({"111": str(avatar)})
            render_group_rank_image(
                out, title="群排行", subtitle="测试", summary=self.summary(
                    [("111", 9), ("222", 4), ("333", 1)]
                ), rank_limit=2, avatar_cache=cache,
            )
            with Image.open(out) as image:
                # first group row center: x=166, y=250; second row center y=326
                self.assertEqual(image.getpixel((166, 250)), (250, 15, 45))
                self.assertNotEqual(image.getpixel((166, 326)), (250, 15, 45))
                # The corners of the avatar box must keep row background.
                self.assertNotEqual(image.getpixel((144, 228)), (250, 15, 45))
            self.assertEqual(cache.calls, ["111", "222"])

    def test_fifty_groups_only_fetches_visible_group_avatars(self):
        entries = [(str(10000+i), 100-i) for i in range(60)]
        with tempfile.TemporaryDirectory() as tmp:
            cache = FakeGroupCache()
            out = Path(tmp) / "rank.png"
            render_group_rank_image(
                out, title="群排行", subtitle="历史", summary=self.summary(entries),
                rank_limit=50, avatar_cache=cache,
            )
            self.assertTrue(out.is_file())
            self.assertEqual(cache.calls, [gid for gid, _ in entries[:50]])

    def test_empty_leaderboard_does_not_fetch_avatar(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FakeGroupCache()
            render_group_rank_image(
                Path(tmp) / "empty.png", title="群排行", subtitle="今天",
                summary=self.summary([]), rank_limit=10, avatar_cache=cache,
            )
            self.assertEqual(cache.calls, [])


if __name__ == "__main__":
    unittest.main()
