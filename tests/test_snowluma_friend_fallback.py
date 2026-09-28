import asyncio
import tempfile
import types
import unittest
from pathlib import Path

try:
    from request_approval import RequestApprovalManager
except ImportError:
    from astrbot_plugin_airi_core.request_approval import RequestApprovalManager


class FakeBot:
    def __init__(self):
        self.calls = []
        self.messages = []

    async def set_friend_add_request(self, **kwargs):
        self.calls.append(("set_friend_add_request", kwargs))

    async def send_private_msg(self, **kwargs):
        self.messages.append(kwargs)

    async def get_stranger_info(self, **kwargs):
        return {"nickname": "申请人"}


class FakeEvent:
    def __init__(
        self,
        sender: str = "2313868490",
        text: str = "请求添加你为好友",
        private: bool = True,
    ):
        self.bot = FakeBot()
        self.message_str = text
        self.message_obj = types.SimpleNamespace(
            raw_message={
                "post_type": "message",
                "message_type": "private" if private else "group",
                "user_id": int(sender),
                "raw_message": text,
            }
        )
        self._sender = sender
        self._private = private
        self.stopped = False

    def get_sender_id(self):
        return self._sender

    def get_self_id(self):
        return "3855327198"

    def is_private_chat(self):
        return self._private

    def stop_event(self):
        self.stopped = True


class SnowlumaFriendFallbackTests(unittest.TestCase):
    def _manager(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return RequestApprovalManager(Path(temp_dir.name) / "pending.json")

    def test_auto_accepts_snowluma_private_prompt_using_sender_uin(self):
        event = FakeEvent()
        manager = self._manager()

        result = asyncio.run(
            manager.handle_request(
                event,
                auto_accept_friend=True,
                approval_enabled=False,
                approval_qq="",
            )
        )

        self.assertEqual(result, "auto_approved_friend")
        self.assertTrue(event.stopped)
        self.assertEqual(
            event.bot.calls,
            [
                (
                    "set_friend_add_request",
                    {"flag": "2313868490", "approve": True},
                )
            ],
        )

    def test_manual_mode_queues_and_notifies_approval_qq(self):
        event = FakeEvent()
        manager = self._manager()

        result = asyncio.run(
            manager.handle_request(
                event,
                auto_accept_friend=False,
                approval_enabled=True,
                approval_qq="2542219495",
            )
        )

        self.assertEqual(result, "queued_friend")
        self.assertTrue(event.stopped)
        self.assertEqual(len(event.bot.messages), 1)
        self.assertEqual(event.bot.messages[0]["user_id"], 2542219495)
        self.assertIn("A001", event.bot.messages[0]["message"])
        self.assertIn("2313868490", event.bot.messages[0]["message"])

    def test_ignores_non_system_text_or_group_message(self):
        manager = self._manager()

        normal_event = FakeEvent(text="你好")
        normal_text = asyncio.run(
            manager.handle_request(
                normal_event,
                auto_accept_friend=True,
                approval_enabled=True,
                approval_qq="2542219495",
            )
        )
        group_event = FakeEvent(private=False)
        group_message = asyncio.run(
            manager.handle_request(
                group_event,
                auto_accept_friend=True,
                approval_enabled=True,
                approval_qq="2542219495",
            )
        )

        self.assertEqual(normal_text, "ignored")
        self.assertFalse(normal_event.stopped)
        self.assertEqual(group_message, "ignored")
        self.assertFalse(group_event.stopped)


if __name__ == "__main__":
    unittest.main()
