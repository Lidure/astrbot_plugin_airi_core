import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from request_approval import PendingRequestStore, RequestApprovalManager


class FakeBot:
    def __init__(self):
        self.friend_calls = []
        self.group_calls = []
        self.private_msgs = []

    async def set_friend_add_request(self, *, flag, approve):
        self.friend_calls.append((flag, approve))

    async def set_group_add_request(self, *, flag, sub_type, approve):
        self.group_calls.append((flag, sub_type, approve))

    async def send_private_msg(self, *, user_id, message):
        self.private_msgs.append((user_id, message))

    async def get_stranger_info(self, *, user_id):
        return {"nickname": f"User{user_id}"}

    async def get_group_info(self, *, group_id):
        return {"group_name": f"Group{group_id}"}


class FakeEvent:
    def __init__(self, raw=None, sender_id="90001"):
        self.bot = FakeBot()
        self.message_obj = SimpleNamespace(raw_message=raw or {})
        self._sender_id = sender_id

    def get_sender_id(self):
        return self._sender_id


class PendingRequestStoreTests(unittest.TestCase):
    def test_persists_pending_request_and_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending_requests.json"
            store = PendingRequestStore(path)
            first = store.add({"kind": "friend", "flag": "f1", "user_id": "10001"})
            second = store.add({"kind": "group_invite", "flag": "g1", "user_id": "10002", "group_id": "20001"})
            self.assertEqual(first["request_id"], "A001")
            self.assertEqual(second["request_id"], "A002")
            reloaded = PendingRequestStore(path)
            self.assertEqual(reloaded.get("A001")["flag"], "f1")
            self.assertEqual(reloaded.get("A002")["group_id"], "20001")

    def test_remove_only_target_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PendingRequestStore(Path(tmp) / "pending_requests.json")
            first = store.add({"kind": "friend", "flag": "f1", "user_id": "10001"})
            second = store.add({"kind": "friend", "flag": "f2", "user_id": "10002"})
            store.remove(first["request_id"])
            self.assertIsNone(store.get(first["request_id"]))
            self.assertIsNotNone(store.get(second["request_id"]))


class RequestApprovalManagerTests(unittest.TestCase):
    def test_auto_accept_friend_uses_direct_client_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RequestApprovalManager(Path(tmp) / "pending_requests.json")
            event = FakeEvent({
                "post_type": "request", "request_type": "friend",
                "flag": "friend-flag", "user_id": 12345, "comment": "hi",
            })
            result = asyncio.run(manager.handle_request(
                event,
                auto_accept_friend=True,
                approval_enabled=True,
                approval_qq="90001",
            ))
            self.assertEqual(event.bot.friend_calls, [("friend-flag", True)])
            self.assertEqual(event.bot.private_msgs, [])
            self.assertEqual(result, "auto_approved_friend")

    def test_friend_request_can_be_forwarded_for_manual_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RequestApprovalManager(Path(tmp) / "pending_requests.json")
            event = FakeEvent({
                "post_type": "request", "request_type": "friend",
                "flag": "friend-flag", "user_id": 12345, "comment": "验证消息",
            })
            result = asyncio.run(manager.handle_request(
                event,
                auto_accept_friend=False,
                approval_enabled=True,
                approval_qq="90001",
            ))
            self.assertEqual(result, "queued_friend")
            self.assertEqual(len(event.bot.private_msgs), 1)
            target, message = event.bot.private_msgs[0]
            self.assertEqual(target, 90001)
            self.assertIn("【好友申请】", message)
            self.assertIn("A001", message)
            self.assertIn("User12345", message)
            self.assertIn("/同意申请 A001", message)
            self.assertIn("/拒绝申请 A001", message)

    def test_group_invite_can_be_forwarded_for_manual_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RequestApprovalManager(Path(tmp) / "pending_requests.json")
            event = FakeEvent({
                "post_type": "request", "request_type": "group", "sub_type": "invite",
                "flag": "group-flag", "user_id": 12345, "group_id": 54321, "comment": "拉你进群",
            })
            result = asyncio.run(manager.handle_request(
                event,
                auto_accept_friend=False,
                approval_enabled=True,
                approval_qq="90001",
            ))
            self.assertEqual(result, "queued_group_invite")
            target, message = event.bot.private_msgs[0]
            self.assertEqual(target, 90001)
            self.assertIn("【群邀请】", message)
            self.assertIn("User12345", message)
            self.assertIn("Group54321", message)

    def test_only_configured_qq_can_approve_and_success_removes_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RequestApprovalManager(Path(tmp) / "pending_requests.json")
            incoming = FakeEvent({
                "post_type": "request", "request_type": "friend",
                "flag": "friend-flag", "user_id": 12345,
            })
            asyncio.run(manager.handle_request(incoming, auto_accept_friend=False, approval_enabled=True, approval_qq="90001"))

            unauthorized = FakeEvent(sender_id="90002")
            unauthorized.bot = incoming.bot
            text = asyncio.run(manager.review(unauthorized, "A001", approve=True, approval_qq="90001"))
            self.assertEqual(text, "你没有审批权限。")
            self.assertIsNotNone(manager.store.get("A001"))

            authorized = FakeEvent(sender_id="90001")
            authorized.bot = incoming.bot
            text = asyncio.run(manager.review(authorized, "A001", approve=True, approval_qq="90001"))
            self.assertIn("已同意好友申请 A001", text)
            self.assertEqual(incoming.bot.friend_calls, [("friend-flag", True)])
            self.assertIsNone(manager.store.get("A001"))

    def test_reject_group_invite_uses_invite_subtype_and_removes_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RequestApprovalManager(Path(tmp) / "pending_requests.json")
            incoming = FakeEvent({
                "post_type": "request", "request_type": "group", "sub_type": "invite",
                "flag": "group-flag", "user_id": 12345, "group_id": 54321,
            })
            asyncio.run(manager.handle_request(incoming, auto_accept_friend=False, approval_enabled=True, approval_qq="90001"))
            reviewer = FakeEvent(sender_id="90001")
            reviewer.bot = incoming.bot
            text = asyncio.run(manager.review(reviewer, "A001", approve=False, approval_qq="90001"))
            self.assertIn("已拒绝群邀请 A001", text)
            self.assertEqual(incoming.bot.group_calls, [("group-flag", "invite", False)])
            self.assertIsNone(manager.store.get("A001"))

    def test_failed_approval_keeps_pending(self):
        class FailingBot(FakeBot):
            async def set_friend_add_request(self, *, flag, approve):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            manager = RequestApprovalManager(Path(tmp) / "pending_requests.json")
            incoming = FakeEvent({
                "post_type": "request", "request_type": "friend",
                "flag": "friend-flag", "user_id": 12345,
            })
            incoming.bot = FailingBot()
            asyncio.run(manager.handle_request(incoming, auto_accept_friend=False, approval_enabled=True, approval_qq="90001"))
            reviewer = FakeEvent(sender_id="90001")
            reviewer.bot = incoming.bot
            text = asyncio.run(manager.review(reviewer, "A001", approve=True, approval_qq="90001"))
            self.assertIn("审批失败", text)
            self.assertIsNotNone(manager.store.get("A001"))


if __name__ == "__main__":
    unittest.main()
