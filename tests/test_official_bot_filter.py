"""QQ official robot filtering tests, including real @ handler behavior."""
from __future__ import annotations

import ast
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from official_bot_filter import OfficialQQBotClassifier, robot_flag
from config_sections import migrate, option

ROOT = Path(__file__).resolve().parents[1]


class FakeEvent:
    def __init__(self, *, group="123456", sender="333333", bot="999999",
                 private=False, raw=None, platform="aiocqhttp", text="来聊天"):
        self.group = group
        self.sender = sender
        self.bot_id = bot
        self.private = private
        self.platform = platform
        self.message_obj = SimpleNamespace(raw_message=raw or {})
        self.message_str = text
        self.parts = [At(bot), Plain(text)]
        self.suppressions = 0
        self.requests = []
        self.unified_msg_origin = "aiocqhttp:GroupMessage:123456"

    def is_private_chat(self): return self.private
    def get_group_id(self): return self.group
    def get_sender_id(self): return self.sender
    def get_self_id(self): return self.bot_id
    def get_platform_name(self): return self.platform
    def get_platform_id(self): return self.platform
    def get_messages(self): return self.parts

    def should_call_llm(self, suppress):
        assert suppress is True
        self.suppressions += 1

    def request_llm(self, **kwargs):
        self.requests.append(kwargs)
        return kwargs


class At:
    def __init__(self, qq): self.qq = qq


class Plain:
    def __init__(self, text): self.text = text


class Image: pass
class Reply: pass


class FakeManager:
    def __init__(self): self.calls = []

    async def get_curr_conversation_id(self, origin):
        self.calls.append(("curr", origin))
        return "existing"

    async def get_conversation(self, origin, cid):
        self.calls.append(("get", origin, cid))
        return {"id": cid}

    async def new_conversation(self, *args):
        raise AssertionError("existing conversation should be reused")


class ClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_only_accepts_boolean_robot_field(self):
        self.assertIs(robot_flag({"is_robot": True}), True)
        self.assertIs(robot_flag({"data": {"is_robot": False}}), False)
        self.assertIsNone(robot_flag({"is_robot": "true"}))
        self.assertIsNone(robot_flag({"is_bot": True}))
        self.assertIsNone(robot_flag({"is_robot": 1}))
        self.assertIsNone(robot_flag({"nickname": "官方机器人"}))

    async def test_authoritative_raw_sender_skips_rpc(self):
        async def unexpected(*args, **kwargs):
            raise AssertionError("No RPC needed")
        classifier = OfficialQQBotClassifier()
        self.assertTrue(await classifier.is_official(
            FakeEvent(raw={"sender": {"is_robot": True}}), unexpected))
        self.assertFalse(await classifier.is_official(
            FakeEvent(raw={"sender": {"is_robot": False}}), unexpected))

    async def test_snowluma_lookup_and_ttl_cache(self):
        calls = []
        clock = [100.0]
        async def call(name, **kwargs):
            calls.append((name, kwargs))
            return {"is_robot": True}
        classifier = OfficialQQBotClassifier(ttl_seconds=60, clock=lambda: clock[0])
        event = FakeEvent()
        self.assertTrue(await classifier.is_official(event, call))
        self.assertTrue(await classifier.is_official(event, call))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("get_group_member_info", {
            "group_id": 123456, "user_id": 333333, "no_cache": False
        }))
        clock[0] = 161.0
        self.assertTrue(await classifier.is_official(event, call))
        self.assertEqual(len(calls), 2)

    async def test_regular_human_is_not_blocked(self):
        calls = []
        async def call(*args, **kwargs):
            calls.append(True)
            return {"data": {"is_robot": False}}
        classifier = OfficialQQBotClassifier()
        event = FakeEvent()
        self.assertFalse(await classifier.is_official(event, call))
        self.assertFalse(await classifier.is_official(event, call))
        self.assertEqual(len(calls), 1)

    async def test_lookup_failure_unknown_or_invalid_ids_never_block(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("OneBot unavailable")
        classifier = OfficialQQBotClassifier()
        self.assertFalse(await classifier.is_official(FakeEvent(), broken))
        self.assertFalse(await classifier.is_official(FakeEvent(), broken))
        self.assertFalse(await classifier.is_official(FakeEvent(), lambda *a, **k: None))
        async def no_flag(*args, **kwargs):
            return {"nickname": "Bot"}
        self.assertFalse(await classifier.is_official(FakeEvent(), no_flag))
        self.assertFalse(await classifier.is_official(FakeEvent(sender="abc"), broken))
        self.assertFalse(await classifier.is_official(FakeEvent(private=True), broken))
        self.assertFalse(await classifier.is_official(FakeEvent(sender="999999"), broken))

    async def test_lookup_timeout_keeps_humans_unblocked(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(1)
            return {"is_robot": True}
        classifier = OfficialQQBotClassifier(timeout_seconds=0.005)
        self.assertFalse(await classifier.is_official(FakeEvent(), slow))


# Use the *real handler implementation* without importing AstrBot dependencies.
module_ast = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
main_class = next(x for x in module_ast.body if isinstance(x, ast.ClassDef) and x.name == "Main")
handler_ast = next(x for x in main_class.body
                   if isinstance(x, ast.AsyncFunctionDef) and x.name == "on_self_mention")
handler_ast.decorator_list = []
module = ast.Module(
    body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), handler_ast],
    type_ignores=[],
)
ast.fix_missing_locations(module)


class NullLogger:
    def error(self, *args): raise AssertionError("Unexpected logger.error")
    def exception(self, *args): raise AssertionError("Unexpected exception")
    def debug(self, *args): pass


async def no_media(*args, **kwargs): return []


class FakePlugin:
    def __init__(self, blocked=False, enabled=True):
        self.blocked = blocked
        self.self_mention_wakeup_enabled = enabled
        self.context = SimpleNamespace(conversation_manager=FakeManager())

    async def _should_ignore_official_qq_bot(self, event):
        return self.blocked


scope = {
    "Comp": SimpleNamespace(At=At, Image=Image, Reply=Reply),
    "collect_mention_image_urls": no_media,
    "quoted_image_placeholder_present": lambda *args, **kwargs: False,
    "quoted_self_message_context": lambda *args, **kwargs: None,
    "logger": NullLogger(),
}
exec(compile(module, "<main.on_self_mention>", "exec"), scope)
on_self_mention = scope["on_self_mention"]


def run_handler(plugin, event):
    async def run():
        return [value async for value in on_self_mention(plugin, event)]
    return asyncio.run(run())


class MentionIntegrationTests(unittest.TestCase):
    def test_official_qq_bot_mention_suppresses_default_and_explicit_llm(self):
        plugin = FakePlugin(blocked=True)
        event = FakeEvent()
        self.assertEqual(run_handler(plugin, event), [])
        self.assertEqual(event.requests, [])
        self.assertEqual(event.suppressions, 1)
        self.assertEqual(plugin.context.conversation_manager.calls, [])

    def test_official_bot_still_filtered_when_custom_at_wakeup_disabled(self):
        plugin = FakePlugin(blocked=True, enabled=False)
        event = FakeEvent()
        self.assertEqual(run_handler(plugin, event), [])
        self.assertEqual(event.suppressions, 1)

    def test_human_mentions_continue_using_original_llm_conversation(self):
        plugin = FakePlugin(blocked=False)
        event = FakeEvent()
        replies = run_handler(plugin, event)
        self.assertEqual(len(replies), 1)
        self.assertEqual(event.suppressions, 1)
        self.assertEqual(event.requests[0]["prompt"], "来聊天")
        self.assertEqual(event.requests[0]["conversation"]["id"], "existing")

    def test_human_custom_wakeup_disabled_does_not_block_default(self):
        plugin = FakePlugin(blocked=False, enabled=False)
        event = FakeEvent()
        self.assertEqual(run_handler(plugin, event), [])
        self.assertEqual(event.suppressions, 0)

    def test_not_mentioned_does_not_lookup_or_suppress(self):
        plugin = FakePlugin(blocked=True)
        event = FakeEvent()
        event.parts = [Plain("普通群消息")]
        self.assertEqual(run_handler(plugin, event), [])
        self.assertEqual(event.suppressions, 0)

    def test_config_default_and_grouped_access(self):
        config = {}
        migrate(config)
        self.assertIs(option(config, "ignore_official_qq_bots_enabled"), True)
        fields = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(fields["chat_settings"]["items"]["ignore_official_qq_bots_enabled"]["default"], True)
