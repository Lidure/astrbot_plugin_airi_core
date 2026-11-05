import json
import unittest
from pathlib import Path

from config_sections import MIGRATION_KEY, PATHS, migrate, option


ROOT = Path(__file__).resolve().parents[1]


class PersistingConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class GroupedConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "_conf_schema.json").read_text("utf-8"))

    def test_schema_groups_visible_in_seven_sections(self):
        groups = {
            k: v for k, v in self.schema.items()
            if v.get("type") == "object" and not v.get("invisible")
        }
        self.assertEqual(
            list(groups),
            ["chat_settings", "moderation_settings", "welcome_settings",
             "request_settings", "poke_settings", "profile_like_settings", "qzone_settings"],
        )
        profile = groups["profile_like_settings"]["items"]
        self.assertEqual(list(profile), ["profile_like_auto_reply_enabled"])
        self.assertFalse(profile["profile_like_auto_reply_enabled"]["default"])
        for obsolete in ("profile_like_reply_times", "profile_like_daily_limit",
                         "profile_like_cooldown_sec"):
            self.assertNotIn(obsolete, self.schema["profile_like_settings"]["items"])
        qzone = groups["qzone_settings"]["items"]
        self.assertEqual(
            list(qzone),
            ["qzone_auto_like_enabled", "qzone_poll_interval_sec",
             "qzone_max_likes_per_cycle", "advanced"],
        )
        self.assertEqual(qzone["advanced"]["items"]["qzone_dry_run"]["default"], False)
        self.assertNotIn("qzone_bot_qq", self.schema)

    def test_flat_settings_remain_invisible_for_upgrade_only(self):
        self.assertTrue(self.schema["poke_stats_enabled"]["invisible"])
        self.assertTrue(self.schema["qzone_auto_like_enabled"]["invisible"])
        self.assertNotIn("profile_like_reply_times", self.schema)
        self.assertNotIn("profile_like_daily_limit", self.schema)
        self.assertNotIn("profile_like_cooldown_sec", self.schema)
        self.assertTrue(self.schema[MIGRATION_KEY]["invisible"])

    def test_migrate_existing_custom_values_and_persist(self):
        conf = PersistingConfig({
            "mention_tool_enabled": False,
            "poke_rank_max_limit": 25,
            "qzone_auto_like_enabled": True,
            "qzone_dry_run": True,
            "qzone_owner_qq": "123456",
            "qzone_poll_interval_sec": 600,
        })
        self.assertTrue(migrate(conf))
        self.assertEqual(option(conf, "mention_tool_enabled"), False)
        self.assertEqual(option(conf, "poke_rank_max_limit"), 25)
        self.assertEqual(option(conf, "qzone_auto_like_enabled"), True)
        self.assertEqual(option(conf, "qzone_dry_run"), True)
        self.assertEqual(option(conf, "qzone_owner_qq"), "123456")
        self.assertEqual(option(conf, "qzone_poll_interval_sec"), 600)
        self.assertEqual(conf.save_count, 1)
        self.assertFalse(migrate(conf))
        self.assertEqual(conf.save_count, 1)

    def test_profile_auto_reply_uses_single_switch_and_ignores_old_knobs(self):
        conf = {
            "profile_like_settings": {
                "profile_like_auto_reply_enabled": True,
                "profile_like_reply_times": 1,
                "profile_like_daily_limit": 1,
                "profile_like_cooldown_sec": 86400,
            }
        }
        migrate(conf)
        self.assertTrue(option(conf, "profile_like_auto_reply_enabled"))
        for obsolete in ("profile_like_reply_times", "profile_like_daily_limit",
                         "profile_like_cooldown_sec"):
            self.assertNotIn(obsolete, PATHS)

    def test_new_install_uses_single_qzone_master_switch(self):
        conf = {}
        migrate(conf)
        self.assertEqual(option(conf, "qzone_auto_like_enabled"), False)
        self.assertEqual(option(conf, "qzone_dry_run"), False)
        conf["qzone_settings"]["qzone_auto_like_enabled"] = True
        self.assertEqual(option(conf, "qzone_auto_like_enabled"), True)
        self.assertEqual(option(conf, "qzone_dry_run"), False)

    def test_migration_does_not_override_explicitly_changed_new_settings(self):
        conf = {
            "poke_rank_max_limit": 50,
            "poke_settings": {"poke_rank_max_limit": 75},
            "qzone_dry_run": True,
            "qzone_settings": {"advanced": {"qzone_dry_run": False}},
        }
        migrate(conf)
        self.assertEqual(option(conf, "poke_rank_max_limit"), 75)
        self.assertEqual(option(conf, "qzone_dry_run"), False)

    def test_unknown_keys_use_legacy_fallback(self):
        conf = {"legacy_other_plugin_field": 10}
        self.assertEqual(option(conf, "legacy_other_plugin_field"), 10)

    def test_current_qq_is_not_a_config_field(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("qzone_bot_qq", source)
        self.assertIn("event.get_self_id()", source)
        self.assertIn("self._qzone_liker.attach_bot", source)


if __name__ == "__main__":
    unittest.main()
