import asyncio
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from qzone_auto_like import (
    QzoneAutoLike, gtk_from_cookie, like_succeeded, parse_mood_links,
)


def mood(qq, fid):
    return f"https://user.qzone.qq.com/{qq}/mood/{fid}"


class FakeBot:
    async def get_friend_list(self):
        return [{"user_id": 111111}, {"user_id": 222222}]

    async def get_cookies(self, *, domain):
        assert domain == "user.qzone.qq.com"
        return {"cookies": "uin=o123; p_skey=test-secret; skey=backup"}


class FakeNetwork:
    def __init__(self):
        self.feed = [mood("111111", "deadbeef")]
        self.friend_posts = {"111111": [mood("111111", "deadbeef")], "222222": []}
        self.calls = []
        self.likes = []
        self.feed_status = 200
        self.like_status = (200, '{"code":0,"message":"ok"}')

    def get(self, url, headers):
        self.calls.append(url)
        assert headers["Cookie"].startswith("uin=")
        args = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "feeds3_html_more" in url:
            return self.feed_status, "\n".join(self.feed)
        posts = self.friend_posts.get(args["hostuin"][0], [])
        return 200, "\n".join(posts) if posts else "friend_data:[]"

    def post(self, url, headers, fields):
        self.likes.append(fields.copy())
        return self.like_status


class QzoneAutoLikeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "qzone_auto_like.json"
        self.net = FakeNetwork()
        self.bot = FakeBot()

    def service(self, **overrides):
        params = {
            "state_path": self.path, "http_get": self.net.get,
            "http_post": self.net.post, "dry_run": False,
            "friend_batch_size": 2, "delay_min_sec": 0, "delay_max_sec": 0,
        }
        params.update(overrides)
        liker = QzoneAutoLike(**params)
        # attach_bot() automatically starts only when enabled=True.
        self.assertTrue(liker.attach_bot(self.bot, "3855327198"))
        return liker

    def test_parses_plain_and_slash_escaped_links_and_deduplicates(self):
        text = (
            mood("111111", "deadbeef")
            + r" https:\/\/user.qzone.qq.com\/222222\/mood\/a1b2c3"
            + " " + mood("111111", "deadbeef")
        )
        self.assertEqual(
            parse_mood_links(text),
            [("111111", "deadbeef"), ("222222", "a1b2c3")],
        )

    def test_cookie_gtk_requires_skey_and_prefers_p_skey(self):
        self.assertEqual(
            gtk_from_cookie("p_skey=a; skey=b"), gtk_from_cookie("p_skey=a")
        )
        with self.assertRaises(ValueError):
            gtk_from_cookie("uin=123")

    def test_like_response_requires_confirmed_success(self):
        self.assertTrue(like_succeeded(200, '{"code":0,"message":"ok"}'))
        self.assertFalse(like_succeeded(200, '{"code":1}'))
        self.assertFalse(like_succeeded(200, "<html>captcha</html>"))
        self.assertFalse(like_succeeded(500, '{"code":0}'))
        self.assertFalse(like_succeeded(200, '{"code":0,"message":"记录成功"}'))

    def test_first_scan_only_indexes_then_likes_new_friend_post_once(self):
        liker = self.service()
        first = asyncio.run(liker.scan_once())
        self.assertEqual(first["liked"], 0)
        self.assertEqual(self.net.likes, [])
        self.assertGreater(first["baseline"], 0)
        self.net.feed.append(mood("222222", "cafe1234"))
        self.net.friend_posts["222222"] = [mood("222222", "cafe1234")]
        second = asyncio.run(liker.scan_once())
        self.assertEqual(second["liked"], 1)
        self.assertEqual(len(self.net.likes), 1)
        self.assertEqual(self.net.likes[0]["fid"], "cafe1234")
        self.assertEqual(asyncio.run(liker.scan_once())["liked"], 0)

    def test_first_post_of_previously_empty_friend_is_liked(self):
        liker = self.service()
        # friend 222222 has a valid but empty feed during the baseline scan.
        asyncio.run(liker.scan_once())
        self.assertIn("friend:222222", liker._state["initialized"])
        self.net.friend_posts["222222"] = [mood("222222", "d00df00d")]
        # The shared feed has not returned this friend: profile fallback must detect it.
        summary = asyncio.run(liker.scan_once())
        self.assertEqual(summary["liked"], 1)
        self.assertEqual(self.net.likes[0]["fid"], "d00df00d")

    def test_persists_seen_records_across_restart(self):
        liker = self.service()
        asyncio.run(liker.scan_once())
        self.net.feed.append(mood("111111", "abc12345"))
        asyncio.run(liker.scan_once())
        self.assertEqual(len(self.net.likes), 1)
        new_liker = self.service()
        asyncio.run(new_liker.scan_once())
        self.assertEqual(len(self.net.likes), 1)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["account"], "3855327198")
        self.assertIn("111111:abc12345", data["seen"])
        self.assertNotIn("p_skey", self.path.read_text(encoding="utf-8"))

    def test_dry_run_does_not_post(self):
        liker = self.service(dry_run=True)
        asyncio.run(liker.scan_once())
        self.net.feed.append(mood("222222", "aabbcc11"))
        summary = asyncio.run(liker.scan_once())
        self.assertEqual(summary["new"], 1)
        self.assertEqual(summary["liked"], 0)
        self.assertEqual(self.net.likes, [])

    def test_403_triggers_cooldown_and_no_mutating_requests(self):
        liker = self.service()
        self.net.feed_status = 403
        asyncio.run(liker.scan_once())
        self.assertIn("安全验证", liker.status())
        requests_before = len(self.net.calls)
        asyncio.run(liker.scan_once())
        self.assertEqual(len(self.net.calls), requests_before)
        self.assertEqual(self.net.likes, [])

    def test_only_configured_account_can_attach(self):
        liker = QzoneAutoLike(self.path, bot_qq="3855327198")
        self.assertFalse(liker.attach_bot(self.bot, "3899840251"))
        self.assertTrue(liker.attach_bot(self.bot, "3855327198"))

    def test_limits_likes_per_scan(self):
        liker = self.service(max_likes_per_cycle=1)
        asyncio.run(liker.scan_once())
        self.net.feed.extend(
            [mood("111111", "abcdef12"), mood("222222", "f00ba123")]
        )
        summary = asyncio.run(liker.scan_once())
        self.assertEqual(summary["liked"], 1)
        self.assertEqual(len(self.net.likes), 1)


if __name__ == "__main__":
    unittest.main()
