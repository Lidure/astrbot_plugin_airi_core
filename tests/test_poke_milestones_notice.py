import asyncio
import copy
import time
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from friend_requests import resolve_onebot_call_action
from poke_milestones import PokeMilestoneStore
from poke_stats import PokeStatsStore, parse_bot_poke_notice
from test_friend_request_integration import load_main


class Notice:
    unified_msg_origin = "qq:GroupMessage:100"

    def __init__(self):
        raw = dict(post_type="notice", notice_type="notify", sub_type="poke",
                   group_id=100, user_id=200, self_id=300, target_id=300)
        self.message_obj = types.SimpleNamespace(raw_message=raw, group_id="100", self_id="300")
        self.bot = types.SimpleNamespace(call_action=AsyncMock())
        self.get_platform_name = Mock(return_value="aiocqhttp")
        self.get_platform_id = Mock(return_value="qq")
        self.is_private_chat = Mock(return_value=False)
        self.get_self_id = Mock(return_value="300")
        self.should_call_llm = Mock()
        self.extra = {}
        self.set_extra = self.extra.__setitem__
        self.get_extra = self.extra.get
        self.request_llm = Mock(side_effect=lambda **kw: types.SimpleNamespace(**kw))


class PokeNoticeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        module, _, cleanup = load_main()
        self.addCleanup(cleanup)
        module.parse_bot_poke_notice = parse_bot_poke_notice
        module.resolve_onebot_call_action = resolve_onebot_call_action
        adapter = types.ModuleType("aiocqhttp_message_event")
        adapter.AiocqhttpMessageEvent = Notice
        self.enter_patch = patch.dict(sys.modules, {
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": adapter})
        self.enter_patch.start()
        self.addCleanup(self.enter_patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plugin = object.__new__(module.Main)
        self.plugin._poke_lock = asyncio.Lock()
        self.plugin.poke_stats_enabled = True
        self.plugin.welcome_enabled = False
        self.plugin._poke_store = PokeStatsStore(Path(tmp.name) / "poke_stats.json")
        self.store = PokeMilestoneStore(Path(tmp.name) / "poke_milestones.json")
        self.plugin._poke_milestones = self.store
        self.store.set_enabled(True)
        self.rule = self.store.save_rule({"name": "首戳", "target": 1, "tolerance": 0,
                                         "prompt": "轻声", "llm_enabled": True})
        self.manager = AsyncMock()
        self.manager.get_curr_conversation_id.return_value = None
        self.manager.new_conversation.return_value = "cid"
        self.plugin.context = types.SimpleNamespace(conversation_manager=self.manager)
        self.event = Notice()

    def actions(self, bot_role, target_role, ban_result={"status": "ok", "retcode": 0}):
        members = []
        if bot_role is not None:
            members.append({"group_id": 100, "user_id": 300, "role": bot_role})
        if target_role is not None:
            members.append({"group_id": 100, "user_id": 200, "role": target_role, "shut_up_timestamp": 0})
        self.event.bot.call_action.side_effect = [members, ban_result]

    async def poke(self):
        return [reply async for reply in self.plugin.on_notice_event(self.event)]

    async def test_owner_can_mute_admin_once_then_reply_in_current_conversation(self):
        self.actions("owner", "admin")
        reply, = await self.poke()
        self.event.bot.call_action.assert_any_await("get_group_member_list", group_id=100, self_id="300", no_cache=True)
        args, kwargs = self.event.bot.call_action.await_args
        self.assertEqual(args, ("set_group_ban",))
        self.assertEqual(kwargs, {"group_id": 100, "user_id": 200, "duration": 60, "self_id": "300"})
        self.assertEqual(set(self.event.request_llm.call_args.kwargs), {"prompt", "conversation"})
        self.assertIn("轻声", reply.prompt)
        self.manager.new_conversation.assert_awaited_once_with(self.event.unified_msg_origin, "qq")
        self.assertIs(reply.conversation, self.manager.get_conversation.return_value)
        # AstrBot copies ProviderRequest when building the agent request.
        self.event.set_extra("provider_request", reply)
        prepared = copy.copy(reply)
        prepared.func_tool = object()
        await self.plugin.restrict_poke_reply_tools(self.event, prepared)
        self.assertIsNone(prepared.func_tool)
        self.event.set_extra("provider_request", object())
        unrelated = types.SimpleNamespace(func_tool=object())
        await self.plugin.restrict_poke_reply_tools(self.event, unrelated)
        self.assertIsNotNone(unrelated.func_tool)
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.store.snapshot()["history"][0]["status"], "success")
        self.assertEqual(self.event.bot.call_action.await_count, 2)
        self.assertEqual(self.plugin._poke_store.group_summary("100", "200")["target_user_count"], 2)

    async def test_permissions_and_unknown_roles_are_silent(self):
        for count, (bot, target) in enumerate(
            [("admin", "admin"), ("admin", "owner"), ("owner", "owner"),
             ("member", "member"), (None, "member"), ("admin", None), ("admin", "?")], 1
        ):
            with self.subTest(bot=bot, target=target):
                self.actions(bot, target)
                self.store.save_rule({**self.rule, "target": count})
                start = self.event.bot.call_action.await_count
                self.assertEqual(await self.poke(), [])
                calls = self.event.bot.call_action.await_args_list[start:]
                self.assertEqual([c.args[0] for c in calls], ["get_group_member_list"])
        self.assertEqual(self.store.snapshot()["history"], [])
        self.event.request_llm.assert_not_called()

    async def test_rejects_failed_or_mismatched_roster(self):
        valid = [
            {"group_id": 100, "user_id": 300, "role": "admin"},
            {"group_id": 100, "user_id": 200, "role": "member"},
        ]
        for count, roster in enumerate([
            {"status": "failed", "retcode": 1, "data": valid},
            [valid[0], {**valid[1], "group_id": 999}],
            [valid[0], valid[1], {**valid[1], "role": "owner"}],
            [valid[0], {"user_id": 200, "role": "member"}],
        ], 1):
            with self.subTest(roster=roster):
                self.store.save_rule({**self.rule, "target": count})
                self.event.bot.call_action.side_effect = [roster, None]
                start = self.event.bot.call_action.await_count
                self.assertEqual(await self.poke(), [])
                self.assertEqual([c.args[0] for c in self.event.bot.call_action.await_args_list[start:]],
                                 ["get_group_member_list"])
        self.assertEqual(self.store.snapshot()["history"], [])
        self.event.request_llm.assert_not_called()

    async def test_admin_can_mute_member_from_wrapped_roster(self):
        self.event.bot.call_action.side_effect = [
            {"status": "ok", "retcode": 0, "data": [
                {"group_id": 100, "user_id": 300, "role": "admin"},
                {"group_id": 100, "user_id": 200, "role": "member"},
            ]}, {"status": "ok", "retcode": 0},
        ]
        self.store.save_rule({**self.rule, "llm_enabled": False})
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.store.snapshot()["history"][0]["status"], "success")
        self.event.request_llm.assert_not_called()
        self.event.should_call_llm.assert_called_once_with(True)

    async def test_overlapping_rules_use_longest_mute_and_one_reply(self):
        self.store.save_rule({"name": "长禁言", "target": 1, "tolerance": 0,
                              "mute_minutes": 3, "llm_enabled": True, "prompt": "长规则"})
        self.actions("admin", "member")
        reply, = await self.poke()
        self.assertIn("长规则", reply.prompt)
        self.assertEqual(self.event.bot.call_action.await_count, 2)
        self.assertEqual(self.event.bot.call_action.await_args.kwargs["duration"], 180)
        self.assertEqual([h["status"] for h in self.store.snapshot()["history"]],
                         ["success", "success"])
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.event.bot.call_action.await_count, 2)

    async def test_role_query_failure_is_ignored(self):
        self.event.bot.call_action.side_effect = TimeoutError()
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.event.bot.call_action.await_count, 1)
        self.assertEqual(self.store.snapshot()["history"], [])

    async def test_explicit_failure_can_retry_after_fresh_permissions(self):
        self.actions("admin", "member", {"status": "failed", "retcode": 100})
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.store.snapshot()["history"][0]["status"], "failed")
        self.event.request_llm.assert_not_called()
        self.actions("admin", "member")
        self.store.save_rule({**self.rule, "llm_enabled": False})
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.store.snapshot()["history"][0]["status"], "success")
        self.event.request_llm.assert_not_called()

    async def test_timeout_after_dispatch_is_not_replayed(self):
        self.actions("admin", "member", TimeoutError())
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.store.snapshot()["history"][0]["status"], "unknown")
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.event.bot.call_action.await_count, 2)

    async def test_empty_ban_result_needs_fresh_readback_before_reply(self):
        members = [{"group_id": 100, "user_id": 300, "role": "admin"},
                   {"group_id": 100, "user_id": 200, "role": "member", "shut_up_timestamp": 0}]
        self.event.bot.call_action.side_effect = [
            members, None, {"group_id": 100, "user_id": 200,
                            "shut_up_timestamp": int(time.time()) + 60}
        ]
        reply, = await self.poke()
        self.assertIn("1 分钟", reply.prompt)
        self.assertEqual(self.store.snapshot()["history"][0]["status"], "success")
        self.event.bot.call_action.assert_awaited_with(
            "get_group_member_info", group_id=100, user_id=200, self_id="300", no_cache=True
        )

    async def test_empty_or_async_ban_without_readback_is_unknown(self):
        members = [{"group_id": 100, "user_id": 300, "role": "admin"},
                   {"group_id": 100, "user_id": 200, "role": "member", "shut_up_timestamp": 0}]
        self.event.bot.call_action.side_effect = [
            members, None, {"group_id": 100, "user_id": 200, "shut_up_timestamp": 0}
        ]
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.store.snapshot()["history"][0]["status"], "unknown")
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.event.bot.call_action.await_count, 3)
        self.event.request_llm.assert_not_called()

    async def test_contradictory_failure_receipt_is_unknown_and_not_replayed(self):
        self.actions("admin", "member", {"status": "failed", "retcode": 0})
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.store.snapshot()["history"][0]["status"], "unknown")
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.event.bot.call_action.await_count, 2)

    async def test_missing_retcode_is_not_confirmed(self):
        self.actions("admin", "member", {"status": "ok"})
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.store.snapshot()["history"][0]["status"], "unknown")
        self.event.request_llm.assert_not_called()

    async def test_conversation_failure_preserves_ban_success(self):
        self.actions("owner", "member")
        self.manager.get_conversation.side_effect = RuntimeError("provider unavailable")
        self.assertEqual(await self.poke(), [])
        self.assertEqual(self.store.snapshot()["history"][0]["status"], "success")
        self.event.request_llm.assert_not_called()

    async def test_non_group_non_onebot_and_stats_disabled(self):
        self.event.is_private_chat.return_value = True
        await self.poke()
        self.event.is_private_chat.return_value = False
        self.store.save_rule({**self.rule, "target": 2})
        self.event.get_platform_name.return_value = "other"
        await self.poke()
        self.event.get_platform_name.return_value = "aiocqhttp"
        self.plugin.poke_stats_enabled = False
        self.store.save_rule({**self.rule, "target": 3})
        await self.poke()
        self.event.bot.call_action.assert_not_awaited()
        self.event.request_llm.assert_not_called()
