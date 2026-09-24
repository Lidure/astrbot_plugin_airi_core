import asyncio
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "friend_requests.py"


def load_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("friend_requests", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FriendRequestTests(unittest.TestCase):
    def test_friend_request_module_exists(self):
        self.assertTrue(MODULE_PATH.exists(), "friend_requests.py must exist")

    def test_enabled_friend_request_is_approved(self):
        module = load_module()
        self.assertIsNotNone(module)
        calls = []

        async def call_action(action, **kwargs):
            calls.append((action, kwargs))

        result = asyncio.run(
            module.maybe_accept_friend_request(
                {
                    "post_type": "request",
                    "request_type": "friend",
                    "flag": "flag-123",
                    "user_id": 10001,
                },
                call_action,
                enabled=True,
            )
        )
        self.assertTrue(result.matched)
        self.assertTrue(result.approved)
        self.assertEqual(result.user_id, "10001")
        self.assertIsNone(result.error)
        self.assertEqual(
            calls,
            [("set_friend_add_request", {"flag": "flag-123", "approve": True})],
        )

    def test_disabled_friend_request_is_ignored(self):
        module = load_module()
        self.assertIsNotNone(module)
        calls = []

        async def call_action(action, **kwargs):
            calls.append((action, kwargs))

        result = asyncio.run(
            module.maybe_accept_friend_request(
                {
                    "post_type": "request",
                    "request_type": "friend",
                    "flag": "flag-123",
                    "user_id": 10001,
                },
                call_action,
                enabled=False,
            )
        )
        self.assertFalse(result.matched)
        self.assertFalse(result.approved)
        self.assertEqual(calls, [])

    def test_non_friend_request_is_ignored(self):
        module = load_module()
        self.assertIsNotNone(module)
        calls = []

        async def call_action(action, **kwargs):
            calls.append((action, kwargs))

        result = asyncio.run(
            module.maybe_accept_friend_request(
                {
                    "post_type": "request",
                    "request_type": "group",
                    "flag": "group-flag",
                    "user_id": 10001,
                },
                call_action,
                enabled=True,
            )
        )
        self.assertFalse(result.matched)
        self.assertFalse(result.approved)
        self.assertEqual(calls, [])

    def test_api_failure_is_captured(self):
        module = load_module()
        self.assertIsNotNone(module)

        async def call_action(action, **kwargs):
            raise RuntimeError("boom")

        result = asyncio.run(
            module.maybe_accept_friend_request(
                {
                    "post_type": "request",
                    "request_type": "friend",
                    "flag": "flag-123",
                    "user_id": 10001,
                },
                call_action,
                enabled=True,
            )
        )
        self.assertTrue(result.matched)
        self.assertFalse(result.approved)
        self.assertEqual(result.user_id, "10001")
        self.assertEqual(result.error, "boom")

    def test_missing_flag_is_ignored(self):
        module = load_module()
        self.assertIsNotNone(module)

        async def call_action(action, **kwargs):
            raise AssertionError("must not call OneBot without a request flag")

        result = asyncio.run(
            module.maybe_accept_friend_request(
                {
                    "post_type": "request",
                    "request_type": "friend",
                    "user_id": 10001,
                },
                call_action,
                enabled=True,
            )
        )
        self.assertFalse(result.matched)
        self.assertFalse(result.approved)


if __name__ == "__main__":
    unittest.main()
