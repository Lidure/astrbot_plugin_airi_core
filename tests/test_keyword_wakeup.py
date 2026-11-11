"""Behavioral tests for QQ keyword wakeup without an AstrBot runtime."""
from __future__ import annotations

import ast
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from config_sections import migrate, option
from keyword_wakeup import (
    allowed_group_sender, allowed_private_sender,
    match_keyword, normalize_allowlist, normalize_keywords,
)

ROOT = Path(__file__).resolve().parents[1]


class MatchTests(unittest.TestCase):
    def test_multiple_words_and_casefold_deduplication(self):
        self.assertEqual(
            normalize_keywords([" 小浅子 ", "Airi", "airi", "小浅子", "桃井爱莉，机器人"]),
            ("小浅子", "airi", "桃井爱莉", "机器人"),
        )
        self.assertEqual(normalize_keywords("猫猫\n小浅子；Airi"), ("猫猫", "小浅子", "airi"))
        self.assertEqual(normalize_keywords([]), ())

    def test_contains_vs_prefix(self):
        words = normalize_keywords(["小浅子", "Airi"])
        self.assertEqual(match_keyword("今天小浅子在吗", words, "contains"), "小浅子")
        self.assertIsNone(match_keyword("今天小浅子在吗", words, "prefix"))
        self.assertEqual(match_keyword("  小浅子，快来！", words, "prefix"), "小浅子")
        self.assertEqual(match_keyword("AIRi hello", words, "prefix"), "airi")
        self.assertIsNone(match_keyword("AIri hello", (), "contains"))
        self.assertIsNone(match_keyword("/小浅子 看图", words, "contains"))
        self.assertIsNone(match_keyword("／小浅子", words, "prefix"))
        self.assertIsNone(match_keyword("  ", words, "contains"))

    def test_group_allowlist_does_not_restrict_members_by_private_qq(self):
        groups = normalize_allowlist(["123456", " 999999 ", "123456"])
        self.assertTrue(allowed_group_sender("123456", "111", "999", groups))
        self.assertTrue(allowed_group_sender("123456", "888", "999", groups))
        self.assertFalse(allowed_group_sender("777777", "111", "999", groups))
        self.assertFalse(allowed_group_sender(None, "111", "999", groups))
        self.assertFalse(allowed_group_sender("123456", "999", "999", groups))
        self.assertTrue(allowed_group_sender("123456", "888", "999", frozenset()))

    def test_private_allowlist_never_uses_group_allowlist(self):
        users = normalize_allowlist(["123", "456"])
        self.assertTrue(allowed_private_sender("123", "999", users))
        self.assertFalse(allowed_private_sender("888", "999", users))
        self.assertFalse(allowed_private_sender("999", "999", frozenset()))
        self.assertFalse(allowed_private_sender(None, "999", frozenset()))
        self.assertTrue(allowed_private_sender("888", "999", frozenset()))

    def test_config_schema_and_safe_defaults(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        fields = schema["chat_settings"]["items"]
        self.assertFalse(fields["keyword_wakeup_enabled"]["default"])
        self.assertFalse(fields["keyword_wakeup_private_enabled"]["default"])
        self.assertEqual(fields["keyword_wakeup_words"]["type"], "list")
        self.assertEqual(fields["keyword_wakeup_mode"]["options"], ["contains", "prefix"])
        self.assertIn("群内任何成员", fields["keyword_wakeup_group_whitelist"]["hint"])
        self.assertIn("仅限制QQ私聊", fields["keyword_wakeup_user_whitelist"]["hint"])
        for key in ("keyword_wakeup_group_whitelist", "keyword_wakeup_user_whitelist"):
            self.assertEqual(fields[key]["type"], "list")
            self.assertEqual(fields[key]["default"], [])
            self.assertEqual(fields[key]["items"]["type"], "string")
        config = {}
        migrate(config)
        self.assertFalse(option(config, "keyword_wakeup_enabled"))
        self.assertFalse(option(config, "keyword_wakeup_private_enabled"))
        self.assertEqual(option(config, "keyword_wakeup_words"), [])
        self.assertEqual(option(config, "keyword_wakeup_mode"), "contains")
        config["chat_settings"]["keyword_wakeup_enabled"] = True
        self.assertTrue(option(config, "keyword_wakeup_enabled"))


# Extract the actual handler from main.py and remove only its decorators. This
# exercises the coroutine logic without installing QQ/AstrBot on CI runners.
tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
plugin_node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Main")
handler_node = next(n for n in plugin_node.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_keyword_wakeup")
handler_node.decorator_list = []
module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), handler_node], type_ignores=[])
ast.fix_missing_locations(module)


class FakePlain:
    def __init__(self, text):
        self.text = text


class FakeAt:
    def __init__(self, qq):
        self.qq = qq


class FakeImage:
    pass


class FakeReply:
    pass


class FakeConversationManager:
    def __init__(self, existing=True):
        self.existing = existing
        self.calls = []

    async def get_curr_conversation_id(self, origin):
        self.calls.append(("curr", origin))
        return "old" if self.existing else None

    async def new_conversation(self, origin, platform):
        self.calls.append(("new", origin, platform))
        return "created"

    async def get_conversation(self, origin, cid):
        self.calls.append(("get", origin, cid))
        return {"id": cid, "persona": "Airi"}


class FakePlugin:
    def __init__(self, *, enabled=True, private_enabled=False,
                 words=("小浅子",), mode="contains",
                 groups=frozenset(), users=frozenset(), manager=None):
        self.keyword_wakeup_enabled = enabled
        self.keyword_wakeup_private_enabled = private_enabled
        self.keyword_wakeup_words = words
        self.keyword_wakeup_mode = mode
        self.keyword_wakeup_group_whitelist = groups
        self.keyword_wakeup_private_whitelist = users
        self.context = SimpleNamespace(conversation_manager=manager or FakeConversationManager())


class FakeEvent:
    def __init__(self, parts=None, *, group="888", sender="123", bot="999",
                 woken=False, private=False):
        self.message_obj = SimpleNamespace(group_id=group)
        self.parts = parts if parts is not None else [FakePlain("小浅子 在吗")]
        self.group, self.sender, self.bot = group, sender, bot
        self.private = private
        self.is_at_or_wake_command = woken
        self.unified_msg_origin = (
            f"aiocqhttp:FriendMessage:{sender}" if private
            else "aiocqhttp:GroupMessage:888"
        )
        self.block_default_calls = 0
        self.requests = []

    def is_private_chat(self): return self.private
    def get_sender_id(self): return self.sender
    def get_self_id(self): return self.bot
    def get_messages(self): return self.parts
    def get_platform_id(self): return "aiocqhttp"
    def should_call_llm(self, block):
        assert block is True
        self.block_default_calls += 1
    def request_llm(self, **kwargs):
        self.requests.append(kwargs)
        return kwargs
    def plain_result(self, value): return value


class NullLogger:
    def exception(self, *a): raise AssertionError("Unexpected handler exception")
    def error(self, *a): raise AssertionError("Unexpected handler error")


async def no_media(*args, **kwargs):
    return []


def run_handler(plugin, event):
    async def collect():
        return [value async for value in on_keyword_wakeup(plugin, event)]
    return asyncio.run(collect())


scope = {
    "Comp": SimpleNamespace(Plain=FakePlain, At=FakeAt, Image=FakeImage, Reply=FakeReply),
    "match_keyword": match_keyword,
    "allowed_group_sender": allowed_group_sender,
    "allowed_private_sender": allowed_private_sender,
    "collect_mention_image_urls": no_media,
    "quoted_image_placeholder_present": lambda *args, **kwargs: False,
    "logger": NullLogger(),
}
exec(compile(module, "<main.on_keyword_wakeup>", "exec"), scope)
on_keyword_wakeup = scope["on_keyword_wakeup"]


class HandlerTests(unittest.TestCase):
    def test_one_match_emits_exactly_one_llm_request_with_existing_conversation(self):
        plugin = FakePlugin()
        event = FakeEvent()
        results = run_handler(plugin, event)
        self.assertEqual(len(results), 1)
        self.assertEqual(event.block_default_calls, 1)
        self.assertEqual(event.requests[0]["prompt"], "小浅子 在吗")
        self.assertEqual(event.requests[0]["conversation"]["id"], "old")
        self.assertEqual(event.requests[0]["image_urls"], [])
        self.assertEqual(plugin.context.conversation_manager.calls, [
            ("curr", event.unified_msg_origin),
            ("get", event.unified_msg_origin, "old"),
        ])
        self.assertNotIn("system_prompt", event.requests[0])
        self.assertNotIn("contexts", event.requests[0])

    def test_creates_conversation_if_none_exists(self):
        plugin = FakePlugin(manager=FakeConversationManager(existing=False))
        event = FakeEvent()
        run_handler(plugin, event)
        self.assertEqual(event.requests[0]["conversation"]["id"], "created")
        self.assertEqual(plugin.context.conversation_manager.calls[1][0], "new")

    def test_contains_mode_notifies_but_prefix_does_not_for_mid_sentence_keyword(self):
        event1 = FakeEvent([FakePlain("今天小浅子在吗？")])
        self.assertEqual(len(run_handler(FakePlugin(), event1)), 1)
        event2 = FakeEvent([FakePlain("今天小浅子在吗？")])
        self.assertEqual(run_handler(FakePlugin(mode="prefix"), event2), [])
        event3 = FakeEvent([FakePlain("  小浅子，陪我聊天")])
        self.assertEqual(len(run_handler(FakePlugin(mode="prefix"), event3)), 1)

    def test_no_llm_when_disabled_restricted_or_self_sent(self):
        cases = [
            (FakePlugin(enabled=False), FakeEvent()),
            (FakePlugin(words=()), FakeEvent()),
            (FakePlugin(groups=frozenset({"555"})), FakeEvent()),
            (FakePlugin(), FakeEvent(group=None)),
            (FakePlugin(), FakeEvent(sender="999")),
            (FakePlugin(), FakeEvent(woken=True)),
            (FakePlugin(), FakeEvent([FakeAt("999"), FakePlain("小浅子 在吗")])),
            (FakePlugin(), FakeEvent([FakePlain("/小浅子 你好")])),
            (FakePlugin(), FakeEvent([FakeReply()])),
        ]
        for plugin, event in cases:
            with self.subTest(parts=event.parts, group=event.group):
                self.assertEqual(run_handler(plugin, event), [])
                self.assertEqual(event.block_default_calls, 0)
                self.assertEqual(event.requests, [])

    def test_group_ignores_private_qq_allowlist_even_when_populated(self):
        plugin = FakePlugin(enabled=True,
                            groups=frozenset({"888"}),
                            users=frozenset({"123"}))
        other_member = FakeEvent(sender="456")
        results = run_handler(plugin, other_member)
        self.assertEqual(len(results), 1)
        self.assertEqual(other_member.block_default_calls, 1)
        self.assertEqual(other_member.requests[0]["prompt"], "小浅子 在吗")

        other_group = FakeEvent(group="333", sender="123")
        self.assertEqual(run_handler(plugin, other_group), [])
        self.assertEqual(other_group.block_default_calls, 0)

    def test_private_requires_independent_switch(self):
        # Native AstrBot marks ordinary QQ private chats as wake messages.
        event = FakeEvent(group=None, private=True, woken=True)
        self.assertEqual(run_handler(FakePlugin(enabled=True), event), [])
        self.assertEqual(event.block_default_calls, 0)
        plugin = FakePlugin(enabled=False, private_enabled=True)
        result = run_handler(plugin, event)
        self.assertEqual(len(result), 1)
        self.assertEqual(event.block_default_calls, 1)
        self.assertEqual(event.requests[0]["prompt"], "小浅子 在吗")
        self.assertEqual(event.requests[0]["conversation"]["id"], "old")
        self.assertEqual(plugin.context.conversation_manager.calls[0],
                         ("curr", "aiocqhttp:FriendMessage:123"))
        self.assertEqual(len(event.requests), 1)

    def test_private_match_modes_and_whitelist(self):
        plugin = FakePlugin(enabled=False, private_enabled=True,
                            words=("airi",), mode="prefix",
                            groups=frozenset({"999999"}), users=frozenset({"123"}))
        allowed = FakeEvent([FakePlain("  AIRI 你好")],
                            group=None, private=True, woken=True)
        self.assertEqual(len(run_handler(plugin, allowed)), 1)
        # Group whitelist does not restrict private conversations.
        self.assertEqual(allowed.requests[0]["prompt"], "AIRI 你好")
        disallowed = FakeEvent(group=None, private=True, sender="555")
        self.assertEqual(run_handler(plugin, disallowed), [])
        nonprefix = FakeEvent([FakePlain("你好 airi")], group=None, private=True)
        self.assertEqual(run_handler(plugin, nonprefix), [])
        normal = FakeEvent(group=None, private=True)
        self.assertEqual(run_handler(FakePlugin(enabled=False, private_enabled=False),
                                     normal), [])
        command = FakeEvent([FakePlain("/airi")], group=None, private=True)
        self.assertEqual(run_handler(plugin, command), [])
        own = FakeEvent(group=None, private=True, sender="999")
        self.assertEqual(run_handler(plugin, own), [])

    def test_private_nonmatching_does_not_intercept_normal_message(self):
        event = FakeEvent([FakePlain("普通私聊不含关键词")],
                          group=None, private=True, woken=True)
        self.assertEqual(run_handler(FakePlugin(private_enabled=True), event), [])
        self.assertEqual(event.block_default_calls, 0)
        self.assertEqual(event.requests, [])

    def test_group_behavior_is_unchanged_by_private_setting(self):
        group_event = FakeEvent(woken=True)
        self.assertEqual(run_handler(FakePlugin(private_enabled=True), group_event), [])
        group_event2 = FakeEvent()
        self.assertEqual(run_handler(FakePlugin(enabled=False, private_enabled=True),
                                     group_event2), [])
        group_event3 = FakeEvent()
        self.assertEqual(len(run_handler(FakePlugin(private_enabled=True),
                                         group_event3)), 1)

    def test_text_only_check_does_not_match_forwarded_or_image_metadata(self):
        event = FakeEvent([FakePlain("普通消息"), FakeReply()])
        self.assertEqual(run_handler(FakePlugin(), event), [])

    def test_allowlist_permitted_sender_and_prefix_retain_original_prompt(self):
        plugin = FakePlugin(words=("airi",), mode="prefix",
                            groups=frozenset({"888"}), users=frozenset({"123"}))
        event = FakeEvent([FakePlain("   AIRI 帮我分析这句话")])
        self.assertEqual(len(run_handler(plugin, event)), 1)
        self.assertEqual(event.requests[0]["prompt"], "AIRI 帮我分析这句话")


if __name__ == "__main__":
    unittest.main()
