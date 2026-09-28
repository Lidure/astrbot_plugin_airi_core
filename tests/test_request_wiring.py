import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RequestWiringSourceTests(unittest.TestCase):
    def test_main_uses_dedicated_request_manager_and_aiocqhttp_request_handler(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("RequestApprovalManager", source)
        self.assertIn('@filter.platform_adapter_type("aiocqhttp")', source)
        self.assertIn("async def on_request", source)
        self.assertIn("self._request_approval.handle_request", source)

    def test_main_exposes_manual_approval_commands(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@filter.command("同意申请")', source)
        self.assertIn('@filter.command("拒绝申请")', source)
        self.assertIn("self._request_approval.review", source)

    def test_old_request_handling_is_removed_from_notice_handler(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        notice_tail = source.split("async def on_notice_event", 1)[1]
        self.assertNotIn('raw_message.get("post_type") == "request"', notice_tail)
        self.assertNotIn("maybe_accept_friend_request", source)


if __name__ == "__main__":
    unittest.main()
