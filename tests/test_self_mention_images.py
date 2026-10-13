"""Behavioral tests for direct/quoted images in @Bot wakeup requests.

Runs without an AstrBot instance: media resolution is exercised with async
fake components, and the real AiriCore handler body is compiled from AST.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mention_media import (
    MAX_MENTION_IMAGES,
    collect_mention_image_urls,
    quoted_image_placeholder_present,
)

ROOT = Path(__file__).resolve().parents[1]


class At:
    def __init__(self, qq):
        self.qq = qq


class Image:
    def __init__(self, path, error=None):
        self.path = path
        self.error = error
        self.called = 0

    async def convert_to_file_path(self):
        self.called += 1
        if self.error:
            raise OSError(self.error)
        return self.path


class Reply:
    def __init__(self, chain=None, message_str="", id="123"):
        self.chain = chain or []
        self.message_str = message_str
        self.id = id


class Plain:
    def __init__(self, text):
        self.text = text


def event(*components, text=""):
    return SimpleNamespace(get_messages=lambda: list(components), message_str=text)


class MentionMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_image_is_converted(self):
        img = Image("/tmp/new-photo.png")
        e = event(img)
        result = await collect_mention_image_urls(e, image_type=Image, reply_type=Reply)
        self.assertEqual(result, ["/tmp/new-photo.png"])
        self.assertEqual(img.called, 1)

    async def test_quoted_embedded_image_skips_network_fallback(self):
        reply = Reply([Plain("hello"), Image("/tmp/reply.png")])
        async def must_not_call(*args):
            raise AssertionError("get_msg fallback unnecessary for embedded image")
        result = await collect_mention_image_urls(event(reply), image_type=Image, reply_type=Reply,
                                                    fallback_extractor=must_not_call)
        self.assertEqual(result, ["/tmp/reply.png"])

    async def test_reply_id_only_recovers_real_image_reference(self):
        reply = Reply([], message_str="[图片]", id="456")
        calls = []
        async def extract(e, component):
            calls.append(component.id)
            return ["/tmp/recovered.jpg"]
        result = await collect_mention_image_urls(event(reply), image_type=Image, reply_type=Reply,
                                                    fallback_extractor=extract)
        self.assertEqual(result, ["/tmp/recovered.jpg"])
        self.assertEqual(calls, ["456"])

    async def test_quoted_placeholder_plain_chain_recovers_image(self):
        reply = Reply([Plain("[图片]")])
        result = await collect_mention_image_urls(event(reply), image_type=Image, reply_type=Reply,
                                                    fallback_extractor=lambda *args: asyncio.sleep(0, result=["https://qq.example/i.jpg"]))
        self.assertEqual(result, ["https://qq.example/i.jpg"])

    async def test_embedded_image_failure_uses_fallback(self):
        img = Image("QQ_file_id", error="media unresolved")
        reply = Reply([img])
        result = await collect_mention_image_urls(event(reply), image_type=Image, reply_type=Reply,
                                                    fallback_extractor=lambda *args: asyncio.sleep(0, result=["/tmp/recovered.jpg"]))
        self.assertEqual(result, ["/tmp/recovered.jpg"])

    async def test_direct_image_failure_does_not_crash_other_images(self):
        e = event(Image("bad", error="expired"), Image("/tmp/good.png"))
        result = await collect_mention_image_urls(e, image_type=Image, reply_type=Reply)
        self.assertEqual(result, ["/tmp/good.png"])

    async def test_multiple_images_dedupe_and_cap(self):
        imgs = [Image(f"/tmp/{i}.png") for i in range(30)]
        imgs.insert(4, Image("/tmp/1.png"))
        result = await collect_mention_image_urls(event(*imgs), image_type=Image, reply_type=Reply)
        self.assertEqual(len(result), MAX_MENTION_IMAGES)
        self.assertEqual(len(set(result)), MAX_MENTION_IMAGES)
        self.assertEqual(result[0], "/tmp/0.png")

    async def test_quoted_fallback_failure_is_isolated(self):
        async def fail(*args):
            raise RuntimeError("QQ unavailable")
        result = await collect_mention_image_urls(event(Reply(message_str="[图片]")),
                                                    image_type=Image, reply_type=Reply, fallback_extractor=fail)
        self.assertEqual(result, [])

    def test_quote_image_placeholder_detection(self):
        self.assertTrue(quoted_image_placeholder_present(event(Reply(message_str="[图片]")),
                                                         reply_type=Reply, image_type=Image))
        self.assertTrue(quoted_image_placeholder_present(event(Reply(chain=[Image("id")])),
                                                         reply_type=Reply, image_type=Image))
        self.assertTrue(quoted_image_placeholder_present(event(Reply(chain=[Plain("[图片]")])),
                                                         reply_type=Reply, image_type=Image))
        self.assertFalse(quoted_image_placeholder_present(event(Reply(chain=[Plain("你好")])),
                                                          reply_type=Reply, image_type=Image))

    async def test_bare_text_unaffected(self):
        called = []
        async def extract(*args):
            called.append(1)
            return []
        result = await collect_mention_image_urls(event(Plain("hello")), image_type=Image,
                                                    reply_type=Reply, fallback_extractor=extract)
        self.assertEqual(result, [])
        self.assertEqual(called, [])


class FakeEvent:
    def __init__(self, text="", chain=(), self_id="123"):
        self.message_str = text
        self.chain = list(chain)
        self.self_id = self_id
        self.unified_msg_origin = "qq:Group:456"
        self.responses = []
        self.llm_count = 0

    def get_self_id(self):
        return self.self_id

    def get_messages(self):
        return self.chain

    def get_platform_id(self):
        return "onebot"

    def should_call_llm(self, enabled):
        self.llm_count += 1
        self.call_llm = enabled

    def plain_result(self, text):
        return SimpleNamespace(kind="plain", text=text)

    def request_llm(self, **kwargs):
        self.responses.append(kwargs)
        return SimpleNamespace(kind="llm", **kwargs)


class ConversationManager:
    def __init__(self):
        self.conversation = SimpleNamespace(history="[]")
        self.calls = []

    async def get_curr_conversation_id(self, origin):
        self.calls.append("lookup")
        return "conversation-1"

    async def new_conversation(self, *args):
        self.calls.append("new")
        return "conversation-1"

    async def get_conversation(self, *args):
        self.calls.append("get")
        return self.conversation


def load_handler():
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Main")
    func = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_self_mention")
    func.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[func], type_ignores=[]))
    cmps = SimpleNamespace(At=At, Image=Image, Reply=Reply)
    from mention_media import collect_mention_image_urls, quoted_image_placeholder_present
    ns = {"Comp": cmps, "logger": logging.getLogger("airi-wakeup-tests"),
          "collect_mention_image_urls": collect_mention_image_urls,
          "quoted_image_placeholder_present": quoted_image_placeholder_present}
    exec(compile(module, filename="main.py", mode="exec"), ns)
    return ns["on_self_mention"]


class MentionHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fn = load_handler()
        self.context = SimpleNamespace(conversation_manager=ConversationManager())
        self.plugin = SimpleNamespace(self_mention_wakeup_enabled=True, context=self.context)

    async def run_handler(self, e):
        return [result async for result in self.fn(self.plugin, e)]

    async def test_at_with_text_and_direct_image_passes_multimodal_request(self):
        e = FakeEvent("这张是什么？", [At("123"), Image("/tmp/photo.png")])
        results = await self.run_handler(e)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "llm")
        self.assertEqual(e.responses[0]["image_urls"], ["/tmp/photo.png"])
        self.assertEqual(e.responses[0]["conversation"], self.context.conversation_manager.conversation)
        self.assertEqual(e.responses[0]["prompt"], "这张是什么？")
        self.assertEqual(e.llm_count, 1)

    async def test_at_with_quoted_embedded_image(self):
        e = FakeEvent("识别引用的图片", [Reply([Image("/tmp/quoted.jpg")]), At("123")])
        results = await self.run_handler(e)
        self.assertEqual(results[0].kind, "llm")
        self.assertEqual(e.responses[0]["image_urls"], ["/tmp/quoted.jpg"])
        self.assertEqual(e.llm_count, 1)

    async def test_quoted_id_fallback_carries_image(self):
        e = FakeEvent("这是什么", [Reply(message_str="[图片]"), At("123")])
        with patch("mention_media.collect_mention_image_urls", new_callable=AsyncMock) as mock:
            mock.return_value = ["/tmp/from-onebot-get-msg.jpg"]
            # The handler uses the function imported in its own namespace:
            self.fn.__globals__["collect_mention_image_urls"] = mock
            try:
                results = await self.run_handler(e)
            finally:
                self.fn.__globals__["collect_mention_image_urls"] = collect_mention_image_urls
        self.assertEqual(results[0].kind, "llm")
        self.assertEqual(e.responses[0]["image_urls"], ["/tmp/from-onebot-get-msg.jpg"])

    async def test_at_image_without_text_uses_attachment_prompt(self):
        e = FakeEvent("", [At("123"), Image("/tmp/image.png")])
        results = await self.run_handler(e)
        self.assertEqual(results[0].kind, "llm")
        self.assertEqual(e.responses[0]["prompt"], "<attachment>")

    async def test_missing_quoted_image_does_not_lie_about_seeing_picture(self):
        e = FakeEvent("帮我识别", [At("123"), Reply(message_str="[图片]")])
        async def fail(*args):
            return []
        with patch("mention_media.extract_quoted_message_images", fail, create=True):
            # The real collector defers the fallback import; replace through
            # handler globals to simulate an unresolved attachment.
            self.fn.__globals__["collect_mention_image_urls"] = fail
            try:
                results = await self.run_handler(e)
            finally:
                self.fn.__globals__["collect_mention_image_urls"] = collect_mention_image_urls
        self.assertEqual(results[0].kind, "plain")
        self.assertIn("重新发送原图", results[0].text)
        self.assertEqual(e.responses, [])
        self.assertEqual(e.llm_count, 1)

    async def test_other_mention_cannot_trigger(self):
        e = FakeEvent("hi", [At("other"), Image("/tmp/photo")])
        self.assertEqual(await self.run_handler(e), [])
        self.assertEqual(e.llm_count, 0)

    async def test_disabled_feature_cannot_trigger(self):
        self.plugin.self_mention_wakeup_enabled = False
        e = FakeEvent("hi", [At("123"), Image("/tmp/photo")])
        self.assertEqual(await self.run_handler(e), [])

    async def test_bare_at_stays_silent(self):
        e = FakeEvent("", [At("123")])
        self.assertEqual(await self.run_handler(e), [])
        self.assertEqual(e.llm_count, 0)

    async def test_plain_text_at_still_uses_same_conversation_and_one_llm(self):
        e = FakeEvent("你好", [At("123"), Plain("你好")])
        results = await self.run_handler(e)
        self.assertEqual(results[0].kind, "llm")
        self.assertEqual(e.responses[0]["image_urls"], [])
        self.assertEqual(e.responses[0]["prompt"], "你好")
        self.assertEqual(e.llm_count, 1)


if __name__ == "__main__":
    unittest.main()
