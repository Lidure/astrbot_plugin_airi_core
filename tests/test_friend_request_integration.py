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

        def info(self, message, *args, **kwargs):
            self.infos.append(str(message))

        def error(self, message, *args, **kwargs):
            self.errors.append(str(message))

        def warning(self, *args, **kwargs):
            pass

        def debug(self, *args, **kwargs):
            pass

        def exception(self, *args, **kwargs):
            pass

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

        @staticmethod
        def platform_adapter_type(*args, **kwargs):
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
    _install_stub("avatar_rank_renderer", render_poke_rank_image=lambda *args, **kwargs: None)
    _install_stub("friend_requests", resolve_onebot_call_action=lambda event: None)
    _install_stub("request_approval", RequestApprovalManager=object)
    _install_stub(
        "poke_stats",
        PokeStatsStore=object,
        extract_onebot_profile_name=lambda *args, **kwargs: "",
        parse_bot_poke_notice=lambda raw: None,
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


class FakeApprovalManager:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def handle_request(self, event, **kwargs):
        self.calls.append((event, kwargs))
        return self.result


class FriendRequestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.module, self.logger, self.cleanup = load_main()

    def tearDown(self):
        self.cleanup()

    def _plugin(self, result, *, auto=True, manual=False, qq="90001"):
        plugin = object.__new__(self.module.Main)
        plugin.auto_accept_friend_request = auto
        plugin.request_approval_enabled = manual
        plugin.request_approval_qq = qq
        plugin._request_approval = FakeApprovalManager(result)
        return plugin

    def test_request_handler_passes_current_settings_to_manager(self):
        plugin = self._plugin("auto_approved_friend", auto=True, manual=True)
        event = object()

        asyncio.run(plugin.on_request(event))

        self.assertEqual(len(plugin._request_approval.calls), 1)
        _, kwargs = plugin._request_approval.calls[0]
        self.assertEqual(
            kwargs,
            {
                "auto_accept_friend": True,
                "approval_enabled": True,
                "approval_qq": "90001",
            },
        )
        self.assertTrue(any("自动同意好友申请" in msg for msg in self.logger.infos))

    def test_manual_queue_result_is_logged(self):
        plugin = self._plugin("queued_group_invite", auto=False, manual=True)
        asyncio.run(plugin.on_request(object()))
        self.assertTrue(any("指定审批 QQ" in msg for msg in self.logger.infos))

    def test_auto_approval_failure_is_logged_without_raising(self):
        plugin = self._plugin("auto_approve_failed", auto=True)
        asyncio.run(plugin.on_request(object()))
        self.assertTrue(any("自动同意好友申请失败" in msg for msg in self.logger.errors))


if __name__ == "__main__":
    unittest.main()
