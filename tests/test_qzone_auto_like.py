import asyncio
import json
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

from qzone_auto_like import (
    QzoneAutoLike, gtk_from_cookie, like_succeeded, like_confirmed, parse_mood_links,
    parse_mood_publish_times, parse_like_state,
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
        self.liked_keys = set()
        self.verify_override = None
        self.verify_status = 200

    def get(self, url, headers):
        self.calls.append(url)
        assert headers["Cookie"].startswith("uin=")
        args = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "get_like_list_app" in url:
            key = args["unikey"][0].removesuffix(".1")
            liked = int(key in self.liked_keys)
            if self.verify_override is not None:
                liked = self.verify_override
            response = {"code": 0, "data": {"is_dolike": liked}}
            return self.verify_status, "_Callback(" + json.dumps(response) + ");"
        if "feeds3_html_more" in url:
            return self.feed_status, "\n".join(self.feed)
        posts = self.friend_posts.get(args["hostuin"][0], [])
        return 200, "\n".join(posts) if posts else "friend_data:[]"

    def post(self, url, headers, fields):
        self.likes.append(fields.copy())
        if (fields["unikey"].startswith("http://user.qzone.qq.com/")
                and like_succeeded(*self.like_status)):
            self.liked_keys.add(fields["unikey"])
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
            "sleep": lambda _: asyncio.sleep(0),
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

    @staticmethod
    def feed_mood(qq, fid, created):
        # Real feeds3_html_more escapes tag quotes as \x22 and sometimes < as \x3C.
        item = (
            f'<i name="feed_data" data-uin="{qq}" '
            f'data-tid="{fid}" data-abstime="{created}"></i>'
            + mood(qq, fid)
        )
        return item.replace('"', r'\x22').replace('<', r'\x3C').replace('>', r'\x3E')

    def test_feed_data_parses_exact_author_id_and_publish_time(self):
        now = int(time.time())
        source = self.feed_mood("111111", "aabbcc22", now - 30)
        matches = parse_mood_publish_times(source)
        self.assertEqual(matches[("111111", "aabbcc22")], now - 30)
        # A matching timestamp in a different author's feed_data cannot
        # accidentally be applied to another author's linked post.
        fake_meta = self.feed_mood("222222", "aabbcc22", now - 60)
        self.assertNotIn(
            ("111111", "aabbcc22"),
            parse_mood_publish_times(fake_meta.replace("222222/mood/", "111111/mood/"))
        )

    def test_like_state_distinguishes_false_from_unavailable(self):
        self.assertIs(
            parse_like_state(200, '_Callback({"code":0,"data":{"is_dolike":0}});'),
            False,
        )
        self.assertIs(
            parse_like_state(200, '_Callback({"code":0,"data":{"is_dolike":1}});'),
            True,
        )
        self.assertIsNone(parse_like_state(200, '{"code":0,"data":{}}'))
        self.assertIsNone(parse_like_state(403, '{"code":0,"data":{"is_dolike":0}}'))

    def test_first_friend_scan_likes_post_newer_than_activation(self):
        liker = self.service(friend_batch_size=1)
        liker._state["started_at"] = time.time() - 3600
        # The main feed knows no posts from friend 2, who is not scanned on cycle 1.
        asyncio.run(liker.scan_once())
        self.net.friend_posts["222222"] = [
            self.feed_mood("222222", "ccaa7788", int(time.time()) - 100)
        ]
        second = asyncio.run(liker.scan_once())
        self.assertEqual(second["liked"], 1)
        self.assertEqual(second["baseline"], 0)
        self.assertEqual(len(self.net.likes), 1)

    def test_first_friend_scan_indexes_pre_activation_history(self):
        liker = self.service(friend_batch_size=1)
        liker._state["started_at"] = time.time() - 3600
        asyncio.run(liker.scan_once())
        self.net.friend_posts["222222"] = [
            self.feed_mood("222222", "ccaa7799", int(time.time()) - 7200)
        ]
        second = asyncio.run(liker.scan_once())
        self.assertEqual(second["liked"], 0)
        self.assertEqual(second["baseline"], 1)

    def test_recovers_previously_baselined_post_from_old_state(self):
        liker = self.service()
        liker._state["started_at"] = time.time() - 3600
        key = "111111:cafeed11"
        liker._state["seen"][key] = time.time() - 90
        liker._save()
        self.net.feed = [self.feed_mood("111111", "cafeed11", int(time.time()) - 120)]
        summary = asyncio.run(liker.scan_once())
        self.assertEqual(summary["recovered"], 1)
        self.assertEqual(summary["liked"], 1)
        self.assertEqual(len(self.net.likes), 1)
        # Even a restart must not repeat an attempt.
        restarted = self.service()
        self.assertEqual(asyncio.run(restarted.scan_once())["liked"], 0)
        self.assertEqual(len(self.net.likes), 1)

    def test_previous_like_already_present_only_marks_resolved(self):
        liker = self.service()
        liker._state["started_at"] = time.time() - 3600
        liker._state["seen"]["111111:bbbbaaaa"] = time.time() - 90
        self.net.feed = [self.feed_mood("111111", "bbbbaaaa", int(time.time()) - 100)]
        self.net.liked_keys.add("http://user.qzone.qq.com/111111/mood/bbbbaaaa")
        summary = asyncio.run(liker.scan_once())
        self.assertEqual(summary["already_liked"], 1)
        self.assertEqual(summary["liked"], 0)
        self.assertEqual(len(self.net.likes), 0)

    def test_upgrade_derives_original_activation_time_from_seen(self):
        older = time.time() - 7200
        self.path.write_text(json.dumps({
            "seen": {"111111:abcd0011": older},
            "initialized": ["feed"], "cursor": 0, "account": "3855327198"
        }), encoding="utf-8")
        liker = self.service()
        self.assertAlmostEqual(liker._state["started_at"], older, delta=1)

    def test_interrupted_old_resolved_is_requeued_and_liked_once(self):
        liker = self.service()
        published = int(time.time()) - 30
        key = "111111:aabbccee"
        self.net.feed = [self.feed_mood("111111", "aabbccee", published)]
        liker._state["started_at"] = time.time() - 3600
        liker._state["seen"][key] = published + 2
        liker._state["resolved"][key] = published + 2
        liker._state["legacy_audit_before"] = time.time()
        liker._save()
        result = asyncio.run(liker.scan_once())
        self.assertEqual(result["legacy_restored"], 1)
        self.assertEqual(result["liked"], 1)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(len(self.net.likes), 1)
        self.assertIn(key, liker._state["legacy_audited"])
        # The same post must not be submitted again, including after restart.
        restart = self.service()
        again = asyncio.run(restart.scan_once())
        self.assertEqual(again["legacy_restored"], 0)
        self.assertEqual(again["attempted"], 0)
        self.assertEqual(len(self.net.likes), 1)

    def test_old_resolved_already_liked_only_reads_and_marks_audited(self):
        liker = self.service()
        published = int(time.time()) - 40
        key = "111111:a1b2c3d4"
        self.net.feed = [self.feed_mood("111111", "a1b2c3d4", published)]
        liker._state["started_at"] = time.time() - 3600
        liker._state["seen"][key] = published + 3
        liker._state["resolved"][key] = published + 3
        liker._state["legacy_audit_before"] = time.time()
        self.net.liked_keys.add("http://user.qzone.qq.com/111111/mood/a1b2c3d4")
        result = asyncio.run(liker.scan_once())
        self.assertEqual(result["legacy_verified"], 1)
        self.assertEqual(result["legacy_restored"], 0)
        self.assertEqual(result["liked"], 0)
        self.assertEqual(len(self.net.likes), 0)
        self.assertIn(key, liker._state["resolved"])
        self.assertIn(key, liker._state["legacy_audited"])

    def test_old_resolved_predating_activation_is_not_recovered(self):
        liker = self.service()
        published = int(time.time()) - 7200
        key = "111111:a1b2c3d5"
        self.net.feed = [self.feed_mood("111111", "a1b2c3d5", published)]
        liker._state["started_at"] = time.time() - 3600
        liker._state["seen"][key] = published + 1
        liker._state["resolved"][key] = published + 1
        result = asyncio.run(liker.scan_once())
        self.assertEqual(result["legacy_restored"], 0)
        self.assertEqual(len(self.net.likes), 0)

    def test_uncertain_legacy_status_stays_resolved_for_future_read(self):
        liker = self.service()
        published = int(time.time()) - 20
        key = "111111:a1b2c3d6"
        self.net.feed = [self.feed_mood("111111", "a1b2c3d6", published)]
        liker._state["started_at"] = time.time() - 3600
        liker._state["seen"][key] = published + 1
        liker._state["resolved"][key] = published + 1
        self.net.verify_status = 500
        result = asyncio.run(liker.scan_once())
        self.assertEqual(result["legacy_restored"], 0)
        self.assertIn(key, liker._state["resolved"])
        self.assertNotIn(key, liker._state["legacy_audited"])
        self.assertEqual(len(self.net.likes), 0)
        self.net.verify_status = 200
        next_round = asyncio.run(liker.scan_once())
        self.assertEqual(next_round["legacy_restored"], 1)
        self.assertEqual(next_round["liked"], 1)

    def test_non_recent_or_newly_resolved_entries_are_not_repaired(self):
        liker = self.service()
        published = int(time.time()) - 20
        key = "111111:a1b2c3d7"
        self.net.feed = [self.feed_mood("111111", "a1b2c3d7", published)]
        liker._state["started_at"] = time.time() - 3600
        liker._state["seen"][key] = published + 2
        # This entry was resolved *after* the audit cut-off: new, verified code.
        liker._state["legacy_audit_before"] = published + 3
        liker._state["resolved"][key] = published + 4
        result = asyncio.run(liker.scan_once())
        self.assertEqual(result["legacy_restored"], 0)
        self.assertEqual(len(self.net.likes), 0)

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

    def test_like_verification_requires_true_is_dolike(self):
        self.assertTrue(
            like_confirmed(
                200, '_Callback({"code":0,"data":{"is_dolike":1}});'
            )
        )
        self.assertFalse(
            like_confirmed(
                200, '_Callback({"code":0,"data":{"is_dolike":0}});'
            )
        )
        self.assertFalse(like_confirmed(200, '{"code":0}'))
        self.assertFalse(like_confirmed(403, '{"code":0,"data":{"is_dolike":1}}'))

    def test_code_zero_without_actual_like_does_not_count_success(self):
        liker = self.service()
        asyncio.run(liker.scan_once())
        self.net.verify_override = 0
        self.net.feed.append(mood("111111", "abeef001"))
        result = asyncio.run(liker.scan_once())
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["liked"], 0)
        self.assertEqual(len(self.net.likes), 1)
        self.assertEqual(asyncio.run(liker.scan_once())["liked"], 0)

    def test_real_like_payload_matches_http_mood_key_and_is_verified(self):
        liker = self.service()
        asyncio.run(liker.scan_once())
        self.net.feed.append(mood("111111", "abcd9876"))
        result = asyncio.run(liker.scan_once())
        self.assertEqual(result["liked"], 1)
        payload = self.net.likes[0]
        self.assertEqual(
            payload["unikey"], "http://user.qzone.qq.com/111111/mood/abcd9876"
        )
        self.assertEqual(payload["curkey"], payload["unikey"])
        self.assertTrue(
            any("get_like_list_app" in url for url in self.net.calls)
        )

    def test_burst_of_eight_posts_is_drained_across_three_scans(self):
        liker = self.service(max_likes_per_cycle=3)
        asyncio.run(liker.scan_once())
        posts = [mood("111111", f"cafe{i:04x}") for i in range(8)]
        self.net.feed.extend(posts)
        self.net.friend_posts["111111"].extend(posts)
        first = asyncio.run(liker.scan_once())
        self.assertEqual(first["new"], 8)
        self.assertEqual(first["queued"], 8)
        self.assertEqual(first["attempted"], 3)
        self.assertEqual(first["liked"], 3)
        self.assertEqual(first["pending"], 5)
        # Posts can disappear from both feeds; queue must not lose them.
        self.net.feed = [mood("111111", "deadbeef")]
        self.net.friend_posts["111111"] = [mood("111111", "deadbeef")]
        second = asyncio.run(liker.scan_once())
        self.assertEqual(second["new"], 0)
        self.assertEqual(second["liked"], 3)
        self.assertEqual(second["pending"], 2)
        third = asyncio.run(liker.scan_once())
        self.assertEqual(third["liked"], 2)
        self.assertEqual(third["pending"], 0)
        self.assertEqual(len(self.net.likes), 8)
        self.assertEqual(len({x["fid"] for x in self.net.likes}), 8)

    def test_pending_queue_survives_restart_even_if_post_disappears(self):
        liker = self.service(max_likes_per_cycle=1)
        asyncio.run(liker.scan_once())
        posts = [mood("111111", f"cafeaa{i:02x}") for i in range(4)]
        self.net.feed.extend(posts)
        first = asyncio.run(liker.scan_once())
        self.assertEqual(first["liked"], 1)
        self.assertEqual(first["pending"], 3)
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk["pending"]), 3)
        self.assertNotIn("test-secret", self.path.read_text(encoding="utf-8"))
        self.net.feed = [mood("111111", "deadbeef")]
        restarted = self.service(max_likes_per_cycle=1)
        summaries = [asyncio.run(restarted.scan_once()) for _ in range(3)]
        self.assertEqual([s["liked"] for s in summaries], [1, 1, 1])
        self.assertEqual(summaries[-1]["pending"], 0)
        self.assertEqual(len(self.net.likes), 4)

    def test_multiple_sources_do_not_enqueue_same_post_twice(self):
        liker = self.service(max_likes_per_cycle=1)
        asyncio.run(liker.scan_once())
        post = mood("111111", "c0ffee11")
        self.net.feed.extend([post, post])
        self.net.friend_posts["111111"].append(post)
        first = asyncio.run(liker.scan_once())
        self.assertEqual(first["queued"], 1)
        self.assertEqual(first["liked"], 1)
        self.assertEqual(asyncio.run(liker.scan_once())["queued"], 0)
        self.assertEqual(len(self.net.likes), 1)

    def test_already_liked_entries_do_not_consume_real_post_quota(self):
        liker = self.service(max_likes_per_cycle=2)
        asyncio.run(liker.scan_once())
        posts = [mood("111111", f"dad0{i:04x}") for i in range(4)]
        self.net.feed.extend(posts)
        # Pre-liked items use a read-only query and must not steal the quota
        # from new items that actually require a like POST.
        for post in posts[:2]:
            self.net.liked_keys.add(post.replace("https://", "http://"))
        summary = asyncio.run(liker.scan_once())
        self.assertEqual(summary["already_liked"], 2)
        self.assertEqual(summary["attempted"], 2)
        self.assertEqual(summary["liked"], 2)
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(len(self.net.likes), 2)

    def test_post_failure_keeps_queue_and_safely_retries_later(self):
        liker = self.service()
        asyncio.run(liker.scan_once())
        self.net.like_status = (200, '{"code":1}')
        self.net.feed.append(mood("111111", "failed11"))
        # Mood IDs must be hexadecimal.
        self.net.feed[-1] = mood("111111", "fadee111")
        first = asyncio.run(liker.scan_once())
        self.assertEqual(first["liked"], 0)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(first["pending"], 1)
        self.assertEqual(first["attempted"], 1)
        second = asyncio.run(liker.scan_once())
        self.assertEqual(second["attempted"], 0)
        self.assertEqual(len(self.net.likes), 1)
        self.net.like_status = (200, '{"code":0}')
        liker._state["pending"][0]["next_retry_at"] = 0
        third = asyncio.run(liker.scan_once())
        self.assertEqual(third["liked"], 1)
        self.assertEqual(third["pending"], 0)
        self.assertEqual(len(self.net.likes), 2)

    def test_ambiguous_like_status_never_posts(self):
        liker = self.service()
        asyncio.run(liker.scan_once())
        self.net.verify_status = 500
        self.net.feed.append(mood("111111", "abcdef01"))
        first = asyncio.run(liker.scan_once())
        self.assertEqual(first["deferred"], 1)
        self.assertEqual(first["attempted"], 0)
        self.assertEqual(first["pending"], 1)
        self.assertEqual(len(self.net.likes), 0)
        self.net.verify_status = 200
        liker._state["pending"][0]["next_retry_at"] = 0
        second = asyncio.run(liker.scan_once())
        self.assertEqual(second["liked"], 1)
        self.assertEqual(second["pending"], 0)

    def test_three_failed_posts_stop_retry_but_remain_visible_in_queue(self):
        liker = self.service()
        asyncio.run(liker.scan_once())
        self.net.like_status = (200, '{"code":1}')
        self.net.feed.append(mood("111111", "badc0ffe"))
        for attempt in range(3):
            if attempt:
                liker._state["pending"][0]["next_retry_at"] = 0
            summary = asyncio.run(liker.scan_once())
            self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(self.net.likes), 3)
        again = asyncio.run(liker.scan_once())
        self.assertEqual(again["pending"], 1)
        self.assertEqual(again["attempted"], 0)
        self.assertEqual(len(self.net.likes), 3)
        self.assertIn("重试已暂停：1", liker.status())

    def test_dry_run_queue_items_are_not_liked_after_switching_live(self):
        liker = self.service(dry_run=True, max_likes_per_cycle=1)
        asyncio.run(liker.scan_once())
        self.net.feed.extend([
            mood("111111", "babe0001"), mood("111111", "babe0002")
        ])
        first = asyncio.run(liker.scan_once())
        self.assertEqual(first["pending"], 1)
        self.assertEqual(first["dry_run"], 1)
        # Queued but not yet handled during a dry run must not unexpectedly
        # be liked when the operator switches to live mode.
        liker.dry_run = False
        second = asyncio.run(liker.scan_once())
        self.assertEqual(second["dry_run"], 1)
        self.assertEqual(second["pending"], 0)
        self.assertEqual(len(self.net.likes), 0)

    def test_queue_is_not_inherited_by_another_bot_account(self):
        liker = self.service(max_likes_per_cycle=1)
        asyncio.run(liker.scan_once())
        self.net.feed.extend([
            mood("111111", "feed0001"), mood("111111", "feed0002")
        ])
        self.assertEqual(asyncio.run(liker.scan_once())["pending"], 1)
        other = QzoneAutoLike(self.path, http_get=self.net.get, http_post=self.net.post)
        self.assertTrue(other.attach_bot(self.bot, "888888"))
        self.assertEqual(len(other._state["pending"]), 0)

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
