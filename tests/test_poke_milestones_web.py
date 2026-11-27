import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from poke_milestones_web import register_milestone_routes


class PokeMilestoneWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Mock()
        self.state = {"enabled": True, "rules": [], "history": []}
        self.store.snapshot.return_value = self.state
        self.web = SimpleNamespace(
            request=SimpleNamespace(json=AsyncMock()),
            json_response=lambda data, *, status_code=200: (status_code, data),
        )
        context = Mock()
        register_milestone_routes(context, "airi", self.store, web=self.web)
        self.routes = {}
        for call in context.register_web_api.call_args_list:
            path, handler, methods, desc = call.args
            self.assertTrue(desc)
            self.assertTrue(path.startswith("/airi/poke-milestones/"))
            self.routes[tuple(methods), path.removeprefix("/airi/poke-milestones/")] = handler

    async def post(self, path, payload):
        self.web.request.json.return_value = payload
        return await self.routes[("POST",), path]()

    async def test_state_and_registered_routes(self):
        self.assertEqual(set(self.routes), {
            (("GET",), "state"), (("POST",), "settings"),
            (("POST",), "rules/save"), (("POST",), "rules/toggle"),
            (("POST",), "rules/delete"),
        })
        self.assertEqual(await self.routes[("GET",), "state"](), (200, self.state))
        self.web.request.json.assert_not_awaited()

    async def test_writes_call_store_and_return_fresh_state(self):
        rule = {"name": "第十次", "target": 10, "tolerance": 2, "mute_minutes": 1,
                "llm_enabled": True, "prompt": "轻轻吐槽", "enabled": True}
        for path, body, method, args in (
            ("settings", {"enabled": False}, "set_enabled", (False,)),
            ("rules/save", {**rule, "confirm_side_effects": True}, "save_rule", (rule,)),
            ("rules/save", {**rule, "id": "abc", "confirm_side_effects": True}, "save_rule", ({**rule, "id": "abc"},)),
            ("rules/toggle", {"id": "abc", "enabled": True, "confirm_side_effects": True}, "set_rule_enabled", ("abc", True)),
            ("rules/delete", {"id": "abc"}, "delete_rule", ("abc",)),
        ):
            with self.subTest(path=path):
                self.store.reset_mock()
                self.assertEqual(await self.post(path, body), (200, self.state))
                getattr(self.store, method).assert_called_once_with(*args)
                expected_snapshot_calls = 2 if path in {"rules/save", "rules/toggle"} else 1
                self.assertEqual(self.store.snapshot.call_count, expected_snapshot_calls)
        self.web.request.json.assert_awaited_with(default=None)

    async def test_enabling_automatic_mute_requires_explicit_confirmation(self):
        code, data = await self.post("settings", {"enabled": True})
        self.assertEqual((code, data["status"]), (409, "error"))
        self.store.set_enabled.assert_not_called()
        self.assertEqual(await self.post("settings", {"enabled": True, "confirm_side_effects": True}), (200, self.state))
        self.store.set_enabled.assert_called_once_with(True)

    async def test_enabling_rule_while_global_switch_is_on_requires_confirmation(self):
        rule = {"name": "第十次", "target": 10, "tolerance": 2, "mute_minutes": 1,
                "llm_enabled": False, "prompt": "", "enabled": True}
        code, data = await self.post("rules/save", rule)
        self.assertEqual((code, data["status"]), (409, "error"))
        self.store.save_rule.assert_not_called()
        code, data = await self.post("rules/toggle", {"id": "abc", "enabled": True})
        self.assertEqual((code, data["status"]), (409, "error"))
        self.store.set_rule_enabled.assert_not_called()

    async def test_malformed_payloads_do_not_mutate(self):
        for path, body in (
            ("settings", None), ("rules/save", []), ("settings", {"enabled": "false"}),
            ("rules/toggle", {"id": "x", "enabled": 1}), ("rules/delete", {"id": ""}),
            ("rules/save", {"id": None}), ("rules/save", {"command": "/mute"}),
        ):
            with self.subTest(path=path, body=body):
                code, data = await self.post(path, body)
                self.assertEqual((code, data["status"]), (400, "error"))
                self.assertTrue(data["message"])
                self.assertEqual(self.store.mock_calls, [])

    async def test_store_errors_are_returned_to_bridge(self):
        self.store.save_rule.side_effect = ValueError("目标次数必须大于零")
        code, data = await self.post("rules/save", {"target": 0, "confirm_side_effects": True})
        self.assertEqual((code, data["message"]), (400, "目标次数必须大于零"))
        self.store.delete_rule.side_effect = KeyError("gone")
        code, data = await self.post("rules/delete", {"id": "gone"})
        self.assertEqual((code, data["status"]), (404, "error"))
        self.store.snapshot.assert_called_once_with()
