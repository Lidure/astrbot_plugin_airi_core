import asyncio
import tempfile
import unittest
from pathlib import Path

import console_api
from poke_stats import PokeStatsStore, privacy_safe_label


class ConsolePokeRanksTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PokeStatsStore(Path(self.temp.name) / "stats.json")
        self.store.record("987654321", "123456789")
        self.store.record("987654321", "123456789")
        self.store.record("987654321", "111223344")
        self.calls = []

    def ranks(self, **kwargs):
        self.assertTrue(hasattr(console_api, "ConsolePokeRanks"), "WebUI rank name resolver is missing")
        return console_api.ConsolePokeRanks(**kwargs)

    async def lookup(self, action, **params):
        self.calls.append((action, params))
        if action == "get_stranger_info":
            if params["user_id"] == 123456789:
                return {"status": "ok", "retcode": 0, "data": {"user_id": 123456789, "nickname": "小爱莉"}}
            return {"status": "failed", "retcode": 100, "data": None}
        if action == "get_group_list":
            return [{"group_id": 987654321, "group_name": "测试群"}]
        self.fail("Unexpected action: " + action)

    async def test_snapshot_keeps_full_numbers_even_without_onebot(self):
        ranks = self.ranks()
        data = ranks.snapshot(self.store)
        self.assertEqual(data["daily_users"][0], {"id": "123456789", "name": "", "count": 2})
        self.assertEqual(data["history_groups"][0]["id"], "987654321")
        self.assertEqual(await ranks.refresh(self.store, None), data)
        self.assertNotIn("123456789", privacy_safe_label("123456789"))

    async def test_resolves_global_qq_nickname_and_group_name_once_then_uses_cache(self):
        ranks = self.ranks()
        data = await ranks.refresh(self.store, self.lookup, self_id="10001")
        self.assertEqual(data["daily_users"][0], {"id": "123456789", "name": "小爱莉", "count": 2})
        self.assertEqual(data["history_users"][0], data["daily_users"][0])
        self.assertEqual(data["daily_users"][1]["id"], "111223344")
        self.assertEqual(data["daily_users"][1]["name"], "")
        self.assertEqual(data["history_groups"][0], {"id": "987654321", "name": "测试群", "count": 3})
        self.assertEqual([a for a, _ in self.calls].count("get_stranger_info"), 2)
        self.assertEqual([a for a, _ in self.calls].count("get_group_list"), 1)
        self.assertTrue(all(params["self_id"] == "10001" for _, params in self.calls))
        await ranks.refresh(self.store, self.lookup, self_id="10001")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(ranks.snapshot(self.store), data)

    async def test_timeout_or_wrong_identity_never_hides_numbers(self):
        ranks = self.ranks(request_timeout=0.02)
        async def stalled(action, **params):
            if action == "get_group_list":
                await asyncio.sleep(5)
            if params.get("user_id") == 123456789:
                return {"user_id": 123456780, "nickname": "错误身份"}
            raise RuntimeError("unavailable")
        data = await asyncio.wait_for(ranks.refresh(self.store, stalled), timeout=1)
        self.assertEqual(data["daily_users"][0], {"id": "123456789", "name": "", "count": 2})
        self.assertEqual(data["daily_groups"][0]["id"], "987654321")
