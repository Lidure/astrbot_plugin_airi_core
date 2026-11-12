"""Regression tests for command-only @Bot replies on AstrBot + other QQ nodes."""
from __future__ import annotations

import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from at_command_guard import (
    command_reason, matched_external_rule, normalized_external_rules,
    recognized_astrbot_command, text_after_direct_at,
)

ROOT = Path(__file__).resolve().parents[1]


class At:
    def __init__(self, qq): self.qq = qq


class Plain:
    def __init__(self, text): self.text = text


class Image: pass
class Reply: pass


class CommandFilter: pass
class CommandGroupFilter: pass
class RegexFilter: pass
class AnyMessageFilter: pass


class FakeEvent:
    def __init__(self, text="你好呀", *, at_first=True, filters=(), extra_parts=()):
        self.message_str = text
        self.parts = ([At("123"), Plain(text)] if at_first
                      else [Plain("你好 "), At("123"), Plain(text)])
        self.parts.extend(extra_parts)
        self.llm_suppressed = False
        self.llm_requests = []
        self.stopped = False
        self.handlers = [SimpleNamespace(event_filters=filters)] if filters else []
        self.unified_msg_origin = "aiocqhttp:GroupMessage:100"

    def get_self_id(self): return "123"
    def get_messages(self): return self.parts
    def get_extra(self, key, default=None):
        if key == "activated_handlers": return self.handlers
        return default
    def should_call_llm(self, flag):
        assert flag is True
        self.llm_suppressed = True
    def request_llm(self, **kwargs):
        self.llm_requests.append(kwargs)
        return kwargs


class MatchTests(unittest.TestCase):
    def test_only_actual_astrbot_commands_not_generic_listeners(self):
        e = FakeEvent("你好", filters=(CommandFilter(),))
        self.assertTrue(recognized_astrbot_command(e))
        e.handlers = [SimpleNamespace(event_filters=(CommandGroupFilter(),))]
        self.assertTrue(recognized_astrbot_command(e))
        e.handlers = [SimpleNamespace(event_filters=(RegexFilter(), AnyMessageFilter()))]
        self.assertFalse(recognized_astrbot_command(e))

    def test_rules_support_exact_whitespace_args_and_explicit_prefix(self):
        rules = normalized_external_rules(["ww帮助", "=manohelp", "^ww", "ww帮助"])
        self.assertEqual(rules, ("ww帮助", "=manohelp", "^ww"))
        self.assertEqual(matched_external_rule("ww帮助", rules), "ww帮助")
        self.assertEqual(matched_external_rule("ww帮助 说明", rules), "ww帮助")
        self.assertEqual(matched_external_rule("manohelp", rules), "=manohelp")
        self.assertEqual(matched_external_rule("manohelp 干什么", rules), None)
        self.assertEqual(matched_external_rule("ww签到", rules), "^ww")
        self.assertEqual(matched_external_rule("今天发ww帮助", rules), None)

    def test_at_must_be_direct_and_commands_only_read_plain_text(self):
        self.assertEqual(text_after_direct_at(
            FakeEvent("ww帮助").parts, "123", at_type=At, plain_type=Plain
        ), "ww帮助")
        self.assertEqual(text_after_direct_at(
            FakeEvent("ww帮助", at_first=False).parts, "123", at_type=At,
            plain_type=Plain
        ), "")
        self.assertEqual(text_after_direct_at(
            [Reply(), At("123"), Plain("ww帮助")], "123",
            at_type=At, plain_type=Plain
        ), "")
        self.assertEqual(text_after_direct_at(
            [At("999"), At("123"), Plain("ww帮助")], "123",
            at_type=At, plain_type=Plain
        ), "")
        self.assertEqual(text_after_direct_at(
            [At("123"), Image(), Plain("ww帮助")], "123",
            at_type=At, plain_type=Plain
        ), "")

    def test_slash_commands_and_unrecognized_plain_chat(self):
        opts = {"at_type": At, "plain_type": Plain, "external_rules": ()}
        self.assertEqual(command_reason(FakeEvent("/help"), "123", **opts), "slash")
        self.assertEqual(command_reason(FakeEvent("／画廊帮助"), "123", **opts), "slash")
        self.assertIsNone(command_reason(FakeEvent("//example.com"), "123", **opts))
        self.assertIsNone(command_reason(FakeEvent("/"), "123", **opts))
        self.assertIsNone(command_reason(FakeEvent("你好，帮我看看"), "123", **opts))
        self.assertIsNone(command_reason(FakeEvent("今天ww帮助会怎么回复"), "123", **opts))
        self.assertEqual(command_reason(
            FakeEvent("普通文字", filters=(CommandFilter(),)), "123", **opts
        ), "astrbot")

    def test_no_external_rule_means_node_command_not_guessed(self):
        e = FakeEvent("ww帮助")
        self.assertIsNone(command_reason(
            e, "123", at_type=At, plain_type=Plain, external_rules=()
        ))
        self.assertEqual(command_reason(
            e, "123", at_type=At, plain_type=Plain,
            external_rules=normalized_external_rules(["ww帮助"])
        ), "external")


tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
main_class = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == "Main")
handler_node = next(x for x in main_class.body
                    if isinstance(x, ast.AsyncFunctionDef) and x.name == "on_self_mention")
handler_node.decorator_list = []
module = ast.fix_missing_locations(ast.Module(body=[handler_node], type_ignores=[]))


async def media_stub(*args, **kwargs): return []


class NullLogger:
    def debug(self, *args): pass
    def exception(self, *args): raise AssertionError("Unexpected LLM handler error")
    def error(self, *args): raise AssertionError("Unexpected LLM handler error")


scope = {
    "Comp": SimpleNamespace(At=At, Plain=Plain, Image=Image, Reply=Reply),
    "logger": NullLogger(),
    "command_reason": command_reason,
    "collect_mention_image_urls": media_stub,
    "quoted_image_placeholder_present": lambda *args, **kwargs: False,
    "quoted_self_message_context": lambda *args, **kwargs: "",
}
exec(compile(module, "<main.on_self_mention>", "exec"), scope)
on_self_mention = scope["on_self_mention"]


class Manager:
    def __init__(self): self.calls = []

    async def get_curr_conversation_id(self, origin):
        self.calls.append("curr")
        return "current"

    async def get_conversation(self, origin, cid):
        self.calls.append("get")
        return {"id": cid}


class FakePlugin:
    def __init__(self, *, rules=(), guard=True, wake=True):
        self.at_command_suppress_llm_enabled = guard
        self.at_command_external_rules = normalized_external_rules(list(rules))
        self.self_mention_wakeup_enabled = wake
        self.context = SimpleNamespace(conversation_manager=Manager())

    async def _should_ignore_official_qq_bot(self, event): return False


def run_handler(plugin, event):
    async def collect():
        return [resp async for resp in on_self_mention(plugin, event)]
    return asyncio.run(collect())


class HandlerTests(unittest.TestCase):
    def test_astrbot_command_only_node_output_no_llm(self):
        plugin = FakePlugin()
        e = FakeEvent("申请头衔", filters=(CommandFilter(),))
        self.assertEqual(run_handler(plugin, e), [])
        self.assertTrue(e.llm_suppressed)
        self.assertFalse(e.stopped)
        self.assertEqual(e.llm_requests, [])
        self.assertEqual(plugin.context.conversation_manager.calls, [])

    def test_external_command_rule_only_suppresses_llm_not_event(self):
        plugin = FakePlugin(rules=("ww帮助", "=manohelp"))
        e = FakeEvent("ww帮助")
        self.assertEqual(run_handler(plugin, e), [])
        self.assertTrue(e.llm_suppressed)
        self.assertFalse(e.stopped)
        self.assertEqual(e.llm_requests, [])

        e2 = FakeEvent("manohelp")
        self.assertEqual(run_handler(plugin, e2), [])
        self.assertTrue(e2.llm_suppressed)

    def test_slash_command_working_without_external_rules(self):
        plugin = FakePlugin()
        e = FakeEvent("/看画廊")
        self.assertEqual(run_handler(plugin, e), [])
        self.assertTrue(e.llm_suppressed)

    def test_normal_at_chat_still_uses_same_llm_conversation(self):
        plugin = FakePlugin(rules=("ww帮助",))
        e = FakeEvent("你好，今天开心吗？")
        result = run_handler(plugin, e)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(e.llm_requests), 1)
        self.assertEqual(e.llm_requests[0]["prompt"], "你好，今天开心吗？")
        self.assertTrue(e.llm_suppressed)

    def test_disabled_guard_does_not_block_command(self):
        plugin = FakePlugin(rules=("ww帮助",), guard=False)
        e = FakeEvent("ww帮助")
        self.assertEqual(len(run_handler(plugin, e)), 1)
        self.assertEqual(len(e.llm_requests), 1)

    def test_guard_also_suppresses_native_llm_if_custom_at_wakeup_is_off(self):
        plugin = FakePlugin(rules=("ww帮助",), wake=False)
        e = FakeEvent("ww帮助")
        self.assertEqual(run_handler(plugin, e), [])
        self.assertTrue(e.llm_suppressed)
