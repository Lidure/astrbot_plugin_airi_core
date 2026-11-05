import asyncio
import json
import tempfile
import time
import unittest
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
        self.assertEqual(
            parse_profile_like_notice(notice(clock, times=5), self_id="99887766"),
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
        return ProfileLikeReturner(self.path, enabled=opts.pop("enabled", True),
                                   clock=self.clock, **opts)

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

    async def test_one_event_attempts_five_batches_ten_each(self):
        liker = self.liker()
        evt = notice(self.clock, times=1)
        self.assertEqual(
            await liker.handle_notice(evt, self.send, self_id="99887766"), "sent"
        )
        self.assertEqual(self.sent, [(11223344, 10)] * 5)
        state = json.loads(self.path.read_text(encoding="utf-8"))
        user = state["accounts"]["99887766"]["users"]["11223344"]
        self.assertEqual(user["submitted"], 50)
        self.assertEqual(user["status"], "sent")
        self.assertEqual(state["version"], 3)
        self.assertNotIn("operator_nick", self.path.read_text(encoding="utf-8"))

    async def test_incoming_multiple_likes_do_not_change_batch_size(self):
        liker = self.liker()
        self.assertEqual(
            await liker.handle_notice(notice(self.clock, times=9), self.send), "sent"
        )
        self.assertEqual(self.sent, [(11223344, 10)] * 5)

    async def test_same_event_replayed_or_liked_again_same_day_is_ignored(self):
        liker = self.liker()
        evt = notice(self.clock)
        self.assertEqual(await liker.handle_notice(evt, self.send), "sent")
        self.assertEqual(await liker.handle_notice(evt, self.send), "already_done")
        self.clock.advance(3600)
        self.assertEqual(
            await liker.handle_notice(notice(self.clock), self.send), "already_done"
        )
        self.assertEqual(len(self.sent), 5)

    async def test_new_day_triggers_one_new_full_round(self):
        liker = self.liker()
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "sent")
        self.clock.advance(86400)
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "sent")
        self.assertEqual(len(self.sent), 10)

    async def test_stale_cross_midnight_replay_is_ignored(self):
        liker = self.liker()
        prior_day = notice(self.clock)
        self.clock.advance(86400)
        self.assertEqual(await liker.handle_notice(prior_day, self.send), "stale")
        self.assertEqual(self.sent, [])

    async def test_restart_preserves_one_per_day_even_without_event_fingerprint(self):
        liker = self.liker()
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "sent")
        another = self.liker()
        self.clock.advance(1800)
        self.assertEqual(
            await another.handle_notice(notice(self.clock), self.send), "already_done"
        )
        self.assertEqual(len(self.sent), 5)

    async def test_legacy_v1_user_already_liked_today_is_not_liked_again(self):
        from datetime import datetime
        today = datetime.fromtimestamp(self.clock()).date().isoformat()
        self.path.write_text(json.dumps({
            "version": 1,
            "accounts": {
                "99887766": {
                    "date": today,
                    "users": {"11223344": {"attempted": 1, "last_at": self.clock()}},
                    "events": {"old-event": self.clock()},
                }
            },
        }), encoding="utf-8")
        liker = self.liker()
        self.assertEqual(
            await liker.handle_notice(notice(self.clock), self.send), "already_done"
        )
        self.assertEqual(
            await liker.handle_notice(notice(self.clock, operator="22334455"), self.send),
            "sent",
        )
        self.assertEqual(len(self.sent), 5)
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("events", state["accounts"]["99887766"])

    async def test_on_first_rejection_stop_and_do_not_repeat_today(self):
        liker = self.liker()
        async def limit(user_id, times):
            self.sent.append((user_id, times))
            return {"status": "failed", "retcode": 1200,
                    "wording": "点赞失败 今日同一好友点赞数已达上限"}
        self.assertEqual(await liker.handle_notice(notice(self.clock), limit), "limit")
        self.clock.advance(360)
        self.assertEqual(
            await liker.handle_notice(notice(self.clock), limit), "already_done"
        )
        self.assertEqual(self.sent, [(11223344, 10)])

    async def test_partial_success_then_rejection_is_recorded_once(self):
        liker = self.liker()
        async def partial(user_id, times):
            self.sent.append((user_id, times))
            if len(self.sent) == 3:
                return {"status": "failed", "retcode": 1200,
                        "wording": "今日同一好友点赞数已达上限"}
            return {"status": "ok", "retcode": 0}
        self.assertEqual(await liker.handle_notice(notice(self.clock), partial), "limit")
        self.assertEqual(self.sent, [(11223344, 10)] * 3)
        state = json.loads(self.path.read_text(encoding="utf-8"))
        user = state["accounts"]["99887766"]["users"]["11223344"]
        self.assertEqual(user["submitted"], 20)
        self.assertEqual(user["status"], "limit")

    async def test_exception_stops_remaining_batches_without_retry(self):
        liker = self.liker()
        async def failed(user_id, times):
            self.sent.append((user_id, times))
            raise TimeoutError("unknown whether QQ applied the like")
        evt = notice(self.clock)
        self.assertEqual(await liker.handle_notice(evt, failed), "failed")
        self.assertEqual(await self.liker().handle_notice(evt, self.send), "already_done")
        self.assertEqual(len(self.sent), 1)

    async def test_cancelled_inflight_batch_is_already_reserved(self):
        liker = self.liker()
        async def cancelled(user_id, times):
            self.sent.append((user_id, times))
            raise asyncio.CancelledError()
        evt = notice(self.clock)
        with self.assertRaises(asyncio.CancelledError):
            await liker.handle_notice(evt, cancelled)
        self.assertEqual(await self.liker().handle_notice(evt, self.send), "already_done")
        self.assertEqual(len(self.sent), 1)

    async def test_concurrent_notifications_same_person_only_one_round(self):
        liker = self.liker()
        async def slow(user_id, times):
            await asyncio.sleep(0.005)
            return await self.send(user_id, times)
        n1 = notice(self.clock, times=1)
        n2 = notice(self.clock, times=2)
        results = await asyncio.gather(
            liker.handle_notice(n1, slow), liker.handle_notice(n2, slow)
        )
        self.assertEqual(sorted(results), ["already_done", "sent"])
        self.assertEqual(len(self.sent), 5)

    async def test_two_people_and_two_bots_are_tracked_independently(self):
        liker = self.liker()
        self.assertEqual(await liker.handle_notice(notice(self.clock), self.send), "sent")
        self.assertEqual(
            await liker.handle_notice(notice(self.clock, operator="22334455"), self.send),
            "sent",
        )
        self.assertEqual(
            await liker.handle_notice(notice(self.clock, bot="12345678"), self.send),
            "sent",
        )
        self.assertEqual(len(self.sent), 15)

    async def test_non_profile_and_stale_notices_do_nothing(self):
        liker = self.liker()
        n1 = notice(self.clock)
        n1["sub_type"] = "poke"
        self.assertEqual(await liker.handle_notice(n1, self.send), "ignored")
        n2 = notice(self.clock)
        n2["time"] = int(self.clock()) - 4000
        self.assertEqual(await liker.handle_notice(n2, self.send), "stale")
        self.assertEqual(self.sent, [])
        self.assertFalse(self.path.exists())



class FakeProfileBot:
    def __init__(self):
        self.voters = []
        self.calls = []
        self.fail_reads = False

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "get_profile_like":
            if self.fail_reads:
                raise RuntimeError("upstream temporarily unavailable")
            return {"status": "ok", "retcode": 0, "data": {
                "voteInfo": {"userInfos": list(self.voters)},
                "favoriteInfo": {"userInfos": [
                    {"uin": 99988877, "latestTime": 1793842200, "count": 50}
                ]},
            }}
        if action == "send_like":
            return {"status": "ok", "retcode": 0, "data": None}
        raise AssertionError("unsupported action " + action)


class ProfileLikePollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "profile_like_replies.json"
        self.clock = FakeClock()
        self.bot = FakeProfileBot()
        self.liker = ProfileLikeReturner(self.path, enabled=True, clock=self.clock)
        self.liker._bot = self.bot
        self.liker._self_qq = "99887766"

    def add_vote(self, uin, seconds_ago=50, count=1):
        self.bot.voters.append({
            "uin": uin,
            "latestTime": int(self.clock() - seconds_ago),
            "count": count,
        })

    def like_calls(self):
        return [v for v in self.bot.calls if v[0] == "send_like"]

    async def test_real_profile_vote_info_is_discovered_and_liked(self):
        self.add_vote(11223344, seconds_ago=120, count=10)
        summary = await self.liker.poll_once()
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(len(self.like_calls()), 5)
        self.assertEqual(
            [v[1]["times"] for v in self.like_calls()],
            [10] * 5,
        )
        get = next(v for v in self.bot.calls if v[0] == "get_profile_like")
        self.assertEqual(get[1]["user_id"], 99887766)
        self.assertEqual(get[1]["count"], 100)
        self.assertEqual(self.liker._state["polls"]["99887766"], self.clock())

    async def test_first_scan_does_not_like_older_history_or_favorites(self):
        self.add_vote(11223344, seconds_ago=3600)
        summary = await self.liker.poll_once()
        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(self.like_calls(), [])
        self.assertFalse(
            "99988877" in self.liker._state["accounts"].get("99887766", {}).get("users", {})
        )

    async def test_new_like_after_baseline_and_delayed_backend_index(self):
        self.assertEqual((await self.liker.poll_once())["sent"], 0)
        self.clock.advance(95)
        # QQ reports a like from before the last cursor, but within overlap.
        self.add_vote(11223344, seconds_ago=115)
        summary = await self.liker.poll_once()
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(len(self.like_calls()), 5)

    async def test_repeated_polls_do_not_duplicate_any_like(self):
        self.add_vote(11223344, seconds_ago=80)
        await self.liker.poll_once()
        self.clock.advance(90)
        summary = await self.liker.poll_once()
        self.assertEqual(summary["already_done"], 1)
        self.assertEqual(len(self.like_calls()), 5)

    async def test_failure_does_not_advance_watermark(self):
        self.bot.fail_reads = True
        self.add_vote(11223344, seconds_ago=120)
        self.assertEqual((await self.liker.poll_once())["sent"], 0)
        self.assertNotIn("99887766", self.liker._state["polls"])
        self.bot.fail_reads = False
        self.assertEqual((await self.liker.poll_once())["sent"], 1)

    async def test_poll_watermark_and_daily_dedupe_survive_restart(self):
        self.add_vote(11223344, seconds_ago=100)
        await self.liker.poll_once()
        new = ProfileLikeReturner(self.path, enabled=True, clock=self.clock)
        new._bot = self.bot
        new._self_qq = "99887766"
        self.clock.advance(100)
        self.add_vote(22334455, seconds_ago=40)
        stats = await new.poll_once()
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(len(self.like_calls()), 10)

    async def test_notification_and_poll_share_one_dedupe_state(self):
        self.add_vote(11223344, seconds_ago=20)
        event = notice(self.clock, operator="11223344", times=1)
        async def post(uid, times):
            return await self.bot.call_action("send_like", user_id=uid, times=times)
        response, stats = await asyncio.gather(
            self.liker.handle_notice(event, post),
            self.liker.poll_once(),
        )
        self.assertIn(response, ("already_done", "sent"))
        self.assertIn(stats["sent"], (0, 1))
        self.assertEqual(len(self.like_calls()), 5)

    async def test_attach_starts_poll_worker_and_close_stops_it(self):
        self.add_vote(11223344, seconds_ago=30)
        self.liker._bot = None
        self.liker._self_qq = ""
        self.assertTrue(self.liker.attach_bot(self.bot, "99887766"))
        self.assertFalse(self.liker.attach_bot(self.bot, "12345678"))
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.like_calls()), 5)
        await self.liker.close()
        self.assertIsNone(self.liker._bot)



if __name__ == "__main__":
    unittest.main()
