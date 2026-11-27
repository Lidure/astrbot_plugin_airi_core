import asyncio
import unittest

from console_api import register_console_routes, requires_confirmation, validate_settings


class FakeRequest:
    def __init__(self, payload=None):
        self.payload = payload

    async def json(self, default=None):
        return self.payload if self.payload is not None else default


class FakeWeb:
    def __init__(self, payload=None):
        self.request = FakeRequest(payload)

    @staticmethod
    def json_response(body, status_code=200):
        return {"body": body, "status_code": status_code}


class FakeContext:
    def __init__(self):
        self.routes = {}

    def register_web_api(self, route, handler, methods, description):
        self.routes[(route, methods[0])] = handler


class FakeLiker:
    enabled = True
    dry_run = True
    _bot = object()

    async def scan_once(self):
        return {"new": 2, "queued": 2, "attempted": 0, "liked": 0, "failed": 0}


class FakeApprovalManager:
    def __init__(self):
        self.calls = []

    async def review_from_web(self, request_id, *, approve, client):
        self.calls.append((request_id, approve, client))
        return "已同意好友申请 A001。"


class FakePlugin:
    def __init__(self):
        self.settings = {
            "auto_accept_friend_request": False,
            "request_approval_enabled": False,
            "welcome_enabled": False,
            "mute_tool_enabled": False,
            "qzone_auto_like_enabled": False,
            "qzone_dry_run": True,
        }
        self._qzone_liker = FakeLiker()
        self.request_approval_qq = "12345"
        self._onebot_bot = object()
        self._request_approval = FakeApprovalManager()

    def console_state(self):
        return {"settings": dict(self.settings), "runtime": {"plugin_loaded": True}}

    async def update_console_settings(self, settings):
        self.settings.update(settings)


class ConsoleApiTests(unittest.TestCase):
    def test_validate_accepts_supported_fields_and_normalizes_lists(self):
        result = validate_settings({
            "self_mention_wakeup_enabled": True,
            "keyword_wakeup_words": [" Airi ", "airi", "小浅子"],
            "keyword_wakeup_mode": "prefix",
            "keyword_wakeup_group_whitelist": ["12345", "12345"],
            "mute_duration_min": 2,
            "mute_duration_max": 20,
        })
        self.assertEqual(result["keyword_wakeup_words"], ["Airi", "小浅子"])
        self.assertEqual(result["keyword_wakeup_group_whitelist"], ["12345"])
        self.assertEqual(result["keyword_wakeup_mode"], "prefix")

    def test_validate_rejects_unknown_types_ranges_and_invalid_ids(self):
        cases = [
            {"not_a_setting": True},
            {"self_mention_wakeup_enabled": 1},
            {"mute_duration_min": 0},
            {"keyword_wakeup_group_whitelist": ["abc"]},
            {"keyword_wakeup_mode": "regex"},
            {"request_approval_qq": "abc"},
            {"qzone_delay_min_sec": 40, "qzone_delay_max_sec": 10},
            {"mute_duration_min": 20, "mute_duration_max": 10},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_settings(payload)

    def test_combined_validation_checks_existing_values_and_approval_target(self):
        with self.assertRaises(ValueError):
            from console_api import validate_combined_settings
            validate_combined_settings({"mute_duration_min": 1, "mute_duration_max": 10}, {"mute_duration_min": 20})
        with self.assertRaises(ValueError):
            validate_combined_settings({"request_approval_qq": "", "request_approval_enabled": False}, {"request_approval_enabled": True})

    def test_side_effects_require_explicit_confirmation(self):
        current = {"auto_accept_friend_request": False, "qzone_auto_like_enabled": False, "qzone_dry_run": False}
        self.assertTrue(requires_confirmation(current, {"auto_accept_friend_request": True}))
        self.assertTrue(requires_confirmation(current, {"qzone_auto_like_enabled": True}))
        self.assertFalse(requires_confirmation(current, {"auto_accept_friend_request": False}))
        self.assertTrue(requires_confirmation({"qzone_auto_like_enabled": True, "qzone_dry_run": True}, {"qzone_dry_run": False}))

    def test_settings_route_rejects_side_effect_without_confirmation(self):
        async def run():
            context, plugin, web = FakeContext(), FakePlugin(), FakeWeb({"settings": {"auto_accept_friend_request": True}})
            register_console_routes(context, "airi", plugin, web=web)
            handler = context.routes[("/airi/console/settings", "POST")]
            response = await handler()
            self.assertEqual(response["status_code"], 409)
            self.assertFalse(plugin.settings["auto_accept_friend_request"])
        asyncio.run(run())

    def test_settings_route_persists_confirmed_values_and_state_route_reads(self):
        async def run():
            context, plugin, web = FakeContext(), FakePlugin(), FakeWeb({
                "settings": {"auto_accept_friend_request": True},
                "confirm_side_effects": True,
            })
            register_console_routes(context, "airi", plugin, web=web)
            save = context.routes[("/airi/console/settings", "POST")]
            response = await save()
            self.assertEqual(response["status_code"], 200)
            self.assertTrue(plugin.settings["auto_accept_friend_request"])
            web.request = FakeRequest()
            state = context.routes[("/airi/console/state", "GET")]
            response = await state()
            self.assertTrue(response["body"]["settings"]["auto_accept_friend_request"])
        asyncio.run(run())

    def test_request_review_route_uses_configured_approval_identity_and_onebot_client(self):
        async def run():
            context, plugin, web = FakeContext(), FakePlugin(), FakeWeb({"request_id": "A001", "approve": True, "confirm": True})
            register_console_routes(context, "airi", plugin, web=web)
            handler = context.routes[("/airi/console/requests/review", "POST")]
            response = await handler()
            self.assertEqual(response["status_code"], 200)
            request_id, approve, bot = plugin._request_approval.calls[0]
            self.assertIs(bot, plugin._onebot_bot)
            self.assertEqual((request_id, approve), ("A001", True))
        asyncio.run(run())

    def test_request_review_route_requires_explicit_confirmation(self):
        async def run():
            context, plugin, web = FakeContext(), FakePlugin(), FakeWeb({"request_id": "A001", "approve": True, "confirm": False})
            register_console_routes(context, "airi", plugin, web=web)
            handler = context.routes[("/airi/console/requests/review", "POST")]
            response = await handler()
            self.assertEqual(response["status_code"], 409)
            self.assertEqual(plugin._request_approval.calls, [])
        asyncio.run(run())

    def test_qzone_manual_scan_requires_enabled_account_and_confirmation_for_real_likes(self):
        async def run():
            context, plugin, web = FakeContext(), FakePlugin(), FakeWeb({})
            register_console_routes(context, "airi", plugin, web=web)
            handler = context.routes[("/airi/console/qzone/scan", "POST")]
            response = await handler()
            self.assertEqual(response["status_code"], 200)
            plugin._qzone_liker.dry_run = False
            web.request = FakeRequest({})
            response = await handler()
            self.assertEqual(response["status_code"], 409)
            web.request = FakeRequest({"confirm_side_effects": True})
            response = await handler()
            self.assertEqual(response["status_code"], 200)
            self.assertEqual(response["body"]["summary"]["new"], 2)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
