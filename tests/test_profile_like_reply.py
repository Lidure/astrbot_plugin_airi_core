import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from profile_like_reply import ProfileLikeReturner, parse_profile_like_notice


class FakeClock:
    def __init__(self, now=None):
        self.now = float(now or time.time())

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def notice(clock, operator="11223344", bot="99887766", times=1, **extra):
    return {
        "post_type": "notice", "notice_type": "notify",
        "sub_type": "profile_like", "self_id": int(bot),
        "operator_id": int(operator), "operator_nick": "someone",
        "times": times, "time": int(clock()),
        **extra,
    }


class ProfileLikeParseTests(unittest.TestCase):
    def test_accepts_real_snowluma_event_shape(self):
        clock = FakeClock()
        evt = notice(clock, times=5)
        self.assertEqual(
            parse_profile_like_notice(evt, self_id="99887766"),
            ("99887766", "11223344", int(clock()), 5),
        )

    def test_rejects_non_profile_like_events(self):
        clock = FakeClock()
        for part in (
            {"notice_type": "group_msg_emoji_like"},
            {"sub_type": "poke"},
            {"post_type": "message"},
            {"operator_id": None},
            {"operator_id": "not-a-qq"},
            {"operator_id": "99887766"},
            {"times": 0},
            {"self_id": 7777},
        ):
            msg = notice(clock)
            msg.update(part)
            with self.subTest(part=part):
                self.assertIsNone(parse_profile_like_notice(msg, self_id="99887766"))

    def test_user_id_does_not_substitute_for_operator_id(self):
        evt = notice(FakeClock())
        evt.pop("operator_id")
        evt["user_id"] = 11223344
        self.assertIsNone(parse_profile_like_notice(evt))


class ProfileLikeReturnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "profile_like_replies.json"
        self.clock = FakeClock()
        self.sent = []

    def liker(self, **opts):
        config = {
            "enabled": True, "clock": self.clock,
            "reply_times": 1, "daily_limit": 10,
            "cooldown_sec": 120,
        }
        config.update(opts)
        return ProfileLikeReturner(self.path, **config)

    async def send(self, user_id, times):
        self.sent.append((user_id, times))
        return {"status": "ok", "retcode": 0, "data": None}

    async def test_disabled_does_not_send(self):
        liker = self.liker(enabled=False)
        self.assertEqual(
            await liker.handle_notice(notice(self.clock), self.send), "disabled"
        )
        self.assertEqual(self.sent, [])
        self.assertFalse(self.path.exists())

    async def test_enabled_replies_once_with_configured_times(self):
        liker = self.liker()
        evt = notice(self.clock, times=9)
        self.assertEqual(
            await liker.handle_notice(evt, self.send, self_id="99887766"), "sent"
        )
        self.assertEqual(self.sent, [(11223344, 1)])
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(state["accounts"]["99887766"]["users"]["11223344"]["attempted"], 1)
        self.assertNotIn("operator_nick", self.path.read_text(encoding="utf-8"))

    async def test_exact_event_duplicate_ignored(self):
        liker = self.liker()
        evt = notice(self.clock)
        self.assertEqual(await liker.handle_notice(evt, self.send), "sent")
        self.assertEqual(await liker.handle_notice(evt, self.send), "duplicate")
        self.assertEqual(len(self.sent), 1)

    async def test_cooldown_does_not_send_second_event(self):
        liker = self.liker(cooldown_sec=120)
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "sent")
        self.clock.advance(30)
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "cooldown")
        self.clock.advance(95)
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "sent")
        self.assertEqual(len(self.sent), 2)

    async def test_daily_limit_clamps_send_times(self):
        liker = self.liker(reply_times=4, daily_limit=5, cooldown_sec=30)
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "sent")
        self.clock.advance(31)
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "sent")
        self.clock.advance(31)
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "daily_limit")
        self.assertEqual(self.sent, [(11223344, 4), (11223344, 1)])

    async def test_new_day_resets_limit(self):
        liker = self.liker(daily_limit=1, cooldown_sec=30)
        evt = notice(self.clock)
        self.assertEqual(await liker.handle_notice(evt, self.send), "sent")
        self.clock.advance(86400)
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "sent")
        self.assertEqual(len(self.sent), 2)

    async def test_restart_preserves_dedupe_and_cooldown(self):
        a = self.liker()
        evt = notice(self.clock)
        self.assertEqual(await a.handle_notice(evt, self.send), "sent")
        b = self.liker()
        self.assertEqual(await b.handle_notice(evt, self.send), "duplicate")
        self.clock.advance(20)
        self.assertEqual(await b.handle_notice(notice(self.clock), self.send), "cooldown")
        self.assertEqual(len(self.sent), 1)

    async def test_api_exception_does_not_repeat_uncertain_request(self):
        liker = self.liker()
        async def send_error(user_id, times):
            self.sent.append((user_id, times))
            raise TimeoutError("lost connection after send")
        evt = notice(self.clock)
        self.assertEqual(await liker.handle_notice(evt, send_error), "failed")
        self.assertEqual(await self.liker().handle_notice(evt, self.send), "duplicate")
        self.assertEqual(len(self.sent), 1)

    async def test_api_failed_retcode_is_not_reported_as_sent(self):
        liker = self.liker()
        async def failed(user_id, times):
            self.sent.append((user_id, times))
            return {"status": "failed", "retcode": 100}
        evt = notice(self.clock)
        self.assertEqual(await liker.handle_notice(evt, failed), "failed")
        self.assertEqual(await liker.handle_notice(evt, failed), "duplicate")

    async def test_ignores_stale_events(self):
        liker = self.liker()
        msg = notice(self.clock)
        msg["time"] = int(self.clock()) - 4000
        self.assertEqual(await liker.handle_notice(msg, self.send), "stale")
        self.assertEqual(self.sent, [])

    async def test_concurrent_same_event_sends_only_once(self):
        liker = self.liker()
        evt = notice(self.clock)
        async def slow(user_id, times):
            await asyncio.sleep(0.01)
            return await self.send(user_id, times)
        results = await asyncio.gather(
            liker.handle_notice(evt, slow), liker.handle_notice(evt, slow)
        )
        self.assertEqual(sorted(results), ["duplicate", "sent"])
        self.assertEqual(len(self.sent), 1)

    async def test_different_bots_use_separate_quotas(self):
        liker = self.liker(daily_limit=1)
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "sent")
        self.assertEqual(
            await liker.handle_notice(notice(self.clock, bot="12345678"), self.send), "sent"
        )
        self.assertEqual(len(self.sent), 2)

    async def test_nonprofile_notice_does_not_touch_state(self):
        liker = self.liker()
        raw = notice(self.clock)
        raw["sub_type"] = "poke"
        self.assertEqual(await liker.handle_notice(raw, self.send), "ignored")
        self.assertEqual(self.sent, [])
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
