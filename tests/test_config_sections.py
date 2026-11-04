import json
import unittest
from pathlib import Path

from config_sections import MIGRATION_KEY, migrate, option


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

    def test_schema_groups_visible_in_six_sections(self):
        groups = {
            k: v for k, v in self.schema.items()
            if v.get("type") == "object" and not v.get("invisible")
        }
        self.assertEqual(
            list(groups),
            ["chat_settings", "moderation_settings", "welcome_settings",
             "request_settings", "poke_settings", "qzone_settings"],
        )
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
