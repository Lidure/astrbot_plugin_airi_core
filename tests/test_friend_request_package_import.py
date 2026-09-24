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


class FriendRequestPackageImportTests(unittest.TestCase):
    def test_main_imports_friend_requests_inside_plugin_package(self):
        original_path = list(sys.path)
        original_modules = dict(sys.modules)
        try:
            sys.path[:] = [
                entry for entry in sys.path
                if Path(entry or ".").resolve() != ROOT
            ]

            _install_stub("pydantic", Field=lambda *args, **kwargs: kwargs.get("default"))
            _install_stub(
                "pydantic.dataclasses",
                dataclass=lambda cls=None, **kwargs: cls
                if cls is not None
                else (lambda c: c),
            )

            class DummyLogger:
                def __getattr__(self, _):
                    return lambda *args, **kwargs: None

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

            _install_stub("astrbot")
            _install_stub("astrbot.api", logger=DummyLogger())
            _install_stub("astrbot.api.event", AstrMessageEvent=object, filter=DummyFilter())
            _install_stub("astrbot.api.star", Context=object, Star=DummyStar)
            _install_stub("astrbot.core")
            _install_stub("astrbot.core.agent")
            _install_stub("astrbot.core.agent.tool", FunctionTool=object)
            _install_stub("astrbot.api.message_components")

            package_name = "airi_core_plugin"
            package = types.ModuleType(package_name)
            package.__path__ = [str(ROOT)]
            sys.modules[package_name] = package
            _install_stub(
                f"{package_name}.poke_stats",
                PokeStatsStore=object,
                extract_onebot_profile_name=lambda *args, **kwargs: "",
                parse_bot_poke_notice=lambda raw: None,
                render_poke_rank_image=lambda *args, **kwargs: None,
            )

            spec = importlib.util.spec_from_file_location(
                f"{package_name}.main", ROOT / "main.py"
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            assert spec.loader is not None
            spec.loader.exec_module(module)

            self.assertIn(f"{package_name}.friend_requests", sys.modules)
            self.assertNotIn("friend_requests", sys.modules)
        finally:
            sys.path[:] = original_path
            for name in list(sys.modules):
                if name not in original_modules:
                    sys.modules.pop(name, None)
            for name, module in original_modules.items():
                sys.modules[name] = module


if __name__ == "__main__":
    unittest.main()
