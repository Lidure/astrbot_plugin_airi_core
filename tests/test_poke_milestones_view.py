import unittest
from html.parser import HTMLParser
from pathlib import Path


VIEW = Path(__file__).resolve().parents[1] / "views" / "Airi控制中心🌸" / "index.html"


class ControlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.keys = set()
        self.scripts = []
        self.nav = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "data-key" in attributes:
            self.keys.add(attributes["data-key"])
        if tag == "script":
            self.scripts.append(attributes)
        if tag == "button" and "data-view" in attributes:
            self.nav.add(attributes["data-view"])


class AiriControlCenterViewTests(unittest.TestCase):
    def setUp(self):
        self.source = VIEW.read_text(encoding="utf-8")
        self.page = ControlParser()
        self.page.feed(self.source)

    def test_one_always_visible_console_covers_all_editable_settings(self):
        self.assertEqual(self.page.nav, {"overview", "chat", "moderation", "requests", "poke", "qzone"})
        self.assertEqual(self.page.keys, {
            "self_mention_wakeup_enabled", "at_command_suppress_llm_enabled",
            "at_command_external_rules", "ignore_official_qq_bots_enabled",
            "keyword_wakeup_enabled", "keyword_wakeup_private_enabled",
            "keyword_wakeup_words", "keyword_wakeup_mode",
            "keyword_wakeup_group_whitelist", "keyword_wakeup_user_whitelist",
            "mention_tool_enabled", "mute_tool_enabled", "mute_duration_min",
            "mute_duration_max", "welcome_enabled", "welcome_message",
            "auto_accept_friend_request", "request_approval_enabled",
            "request_approval_qq", "poke_stats_enabled", "poke_rank_max_limit",
            "qzone_auto_like_enabled", "qzone_poll_interval_sec",
            "qzone_max_likes_per_cycle", "qzone_dry_run", "qzone_owner_qq",
            "qzone_friend_batch_size", "qzone_feed_count", "qzone_delay_min_sec",
            "qzone_delay_max_sec",
        })
        self.assertNotIn("webui_enabled", self.page.keys)

    def test_bridge_routes_and_milestone_editor_are_embedded(self):
        self.assertIn("window.AstrBotPluginView", self.source)
        self.assertIn('apiGet(ROUTES.state)', self.source)
        self.assertIn('apiPost(ROUTES.settings', self.source)
        for route in ("console/state", "console/settings", "console/qzone/scan", "poke-milestones/state", "poke-milestones/rules/save"):
            self.assertIn(route, self.source)
        for control in ("milestone-global", "milestone-add", "milestone-history", "requests-table", "qzone-scan"):
            self.assertIn(f'id="{control}"', self.source)
        self.assertTrue(self.page.scripts)
        self.assertEqual([script["src"] for script in self.page.scripts if "src" in script], ["/api/plugin/page/bridge-sdk.js"])
        self.assertNotIn(".innerHTML", self.source)

    def test_bridge_sdk_loads_before_inline_initialization(self):
        # AstrBot 4.29.3 retargets an explicit canonical SDK URL to the
        # authenticated asset URL; if absent it injects it after inline JS.
        # The parser must execute the SDK synchronously before the app
        # captures window.AstrBotPluginView.
        scripts = self.page.scripts
        self.assertEqual(scripts[0].get("src"), "/api/plugin/page/bridge-sdk.js")
        self.assertNotIn("async", scripts[0])
        self.assertNotIn("defer", scripts[0])
        self.assertNotIn("type", scripts[0])
        self.assertTrue(any("src" not in script for script in scripts[1:]))

    def test_no_separate_poke_only_view_remains(self):
        old_view = VIEW.parents[1] / "poke-milestones"
        self.assertFalse(old_view.exists())


if __name__ == "__main__":
    unittest.main()
