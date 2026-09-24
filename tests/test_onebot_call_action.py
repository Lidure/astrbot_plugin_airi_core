import types
import unittest

from friend_requests import resolve_onebot_call_action


class DirectBot:
    async def call_action(self, action, **kwargs):
        return action, kwargs


class LegacyAPI:
    async def call_action(self, action, **kwargs):
        return action, kwargs


class ResolveOneBotCallActionTests(unittest.TestCase):
    def test_prefers_direct_cqhttp_call_action(self):
        direct = DirectBot()
        event = types.SimpleNamespace(bot=direct)
        self.assertIs(resolve_onebot_call_action(event).__self__, direct)

    def test_falls_back_to_legacy_bot_api_shape(self):
        api = LegacyAPI()
        event = types.SimpleNamespace(bot=types.SimpleNamespace(api=api))
        self.assertIs(resolve_onebot_call_action(event).__self__, api)

    def test_returns_none_without_onebot_client(self):
        event = types.SimpleNamespace(bot=object())
        self.assertIsNone(resolve_onebot_call_action(event))


if __name__ == "__main__":
    unittest.main()
