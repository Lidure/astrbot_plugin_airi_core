import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def load_main():
    original_modules = dict(sys.modules)

    class DummyLogger:
        def __init__(self):
            self.infos = []
            self.errors = []
            self.warnings = []

        def info(self, message, *args, **kwargs):
            self.infos.append(str(message))

        def error(self, message, *args, **kwargs):
            self.errors.append(str(message))

        def warning(self, message, *args, **kwargs):
            self.warnings.append(str(message))

        def debug(self, *args, **kwargs):
            pass

        def exception(self, message, *args, **kwargs):
            self.errors.append(str(message))

    logger = DummyLogger()

    class DummyFilter:
        class EventMessageType:
            ALL = object()

        @staticmethod
        def command(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda func: func

    class DummyStar:
        def __init__(self, *args, **kwargs):
            pass

    _install_stub("pydantic", Field=lambda *args, **kwargs: kwargs.get("default"))
    _install_stub(
        "pydantic.dataclasses",
        dataclass=lambda cls=None, **kwargs: cls if cls is not None else (lambda c: c),
    )
    _install_stub("astrbot")
    _install_stub("astrbot.api", logger=logger)
    _install_stub("astrbot.api.event", AstrMessageEvent=object, filter=DummyFilter())
    _install_stub("astrbot.api.star", Context=object, Star=DummyStar)
    _install_stub("astrbot.core")
    _install_stub("astrbot.core.agent")
    _install_stub("astrbot.core.agent.tool", FunctionTool=object)
    _install_stub("astrbot.api.message_components")
    _install_stub(
        "poke_stats",
        PokeStatsStore=object,
        extract_onebot_profile_name=lambda *args, **kwargs: "",
        parse_bot_poke_notice=lambda raw: None,
        render_poke_rank_image=lambda *args, **kwargs: None,
    )

    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("airi_core_main_under_test", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    def cleanup():
        sys.path[:] = [entry for entry in sys.path if entry != str(ROOT)]
        for name in list(sys.modules):
            if name not in original_modules:
                sys.modules.pop(name, None)
        for name, value in original_modules.items():
            sys.modules[name] = value

    return module, logger, cleanup


class FakeAPI:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.fail:
            raise RuntimeError("OneBot failed")


class FakeEvent:
    def __init__(self, raw_message, api):
        self.message_obj = types.SimpleNamespace(raw_message=raw_message)
        self.bot = types.SimpleNamespace(api=api)


async def drain_async_generator(generator):
    return [item async for item in generator]


class FriendRequestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.module, self.logger, self.cleanup = load_main()

    def tearDown(self):
        self.cleanup()

    def _plugin(self, enabled):
        plugin = object.__new__(self.module.Main)
        plugin.auto_accept_friend_request = enabled
        return plugin

    def test_enabled_friend_request_calls_onebot_approve(self):
        api = FakeAPI()
        event = FakeEvent(
            {
                "post_type": "request",
                "request_type": "friend",
                "flag": "friend-flag",
                "user_id": 123456,
            },
            api,
        )
        plugin = self._plugin(True)

        asyncio.run(drain_async_generator(plugin.on_notice_event(event)))

        self.assertEqual(
            api.calls,
            [("set_friend_add_request", {"flag": "friend-flag", "approve": True})],
        )

    def test_disabled_friend_request_does_not_call_onebot(self):
        api = FakeAPI()
        event = FakeEvent(
            {
                "post_type": "request",
                "request_type": "friend",
                "flag": "friend-flag",
                "user_id": 123456,
            },
            api,
        )
        plugin = self._plugin(False)

        asyncio.run(drain_async_generator(plugin.on_notice_event(event)))

        self.assertEqual(api.calls, [])

    def test_non_friend_request_does_not_call_onebot(self):
        api = FakeAPI()
        event = FakeEvent(
            {
                "post_type": "request",
                "request_type": "group",
                "flag": "group-flag",
                "user_id": 123456,
            },
            api,
        )
        plugin = self._plugin(True)

        asyncio.run(drain_async_generator(plugin.on_notice_event(event)))

        self.assertEqual(api.calls, [])

    def test_onebot_failure_is_logged_and_does_not_escape(self):
        api = FakeAPI(fail=True)
        event = FakeEvent(
            {
                "post_type": "request",
                "request_type": "friend",
                "flag": "friend-flag",
                "user_id": 123456,
            },
            api,
        )
        plugin = self._plugin(True)

        asyncio.run(drain_async_generator(plugin.on_notice_event(event)))

        self.assertEqual(len(api.calls), 1)
        self.assertTrue(any("OneBot failed" in message for message in self.logger.errors))


if __name__ == "__main__":
    unittest.main()
