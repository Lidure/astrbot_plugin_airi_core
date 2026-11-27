import asyncio
import unittest

from main import Main


class Config(dict):
    def __init__(self):
        super().__init__()
        self.saved = False

    async def save_config_async(self):
        self.saved = True
        return True


class ToolManager:
    def __init__(self):
        self.func_list = []


class Context:
    def __init__(self):
        self.manager = ToolManager()
        self.active = set()

    def get_llm_tool_manager(self):
        return self.manager

    def add_llm_tools(self, *tools):
        self.manager.func_list.extend(tools)
        self.active.update(tool.name for tool in tools)

    async def activate_llm_tool_async(self, name):
        self.active.add(name)
        return True

    async def deactivate_llm_tool_async(self, name):
        self.active.discard(name)
        return True


class Liker:
    def __init__(self):
        self.enabled = False
        self.dry_run = True
        self.interval = 300
        self.batch_size = 8
        self.feed_count = 30
        self.max_likes = 3
        self.delay_min = 15
        self.delay_max = 30
        self.started = 0
        self.stopped = 0

    def start(self):
        self.enabled = True
        self.started += 1

    def stop(self):
        self.enabled = False
        self.stopped += 1


class ConsoleRuntimeTests(unittest.TestCase):
    def test_saved_settings_apply_immediately_to_runtime_and_tools(self):
        async def run():
            plugin = Main.__new__(Main)
            plugin.config = Config()
            plugin._console_settings_lock = asyncio.Lock()
            plugin.context = Context()
            plugin._qzone_liker = Liker()
            plugin.self_mention_wakeup_enabled = True
            plugin.at_command_suppress_llm_enabled = True
            plugin.at_command_external_rules = ()
            plugin.ignore_official_qq_bots_enabled = True
            plugin.keyword_wakeup_enabled = False
            plugin.keyword_wakeup_private_enabled = False
            plugin.keyword_wakeup_words = ()
            plugin.keyword_wakeup_mode = "contains"
            plugin.keyword_wakeup_group_whitelist = frozenset()
            plugin.keyword_wakeup_private_whitelist = frozenset()
            plugin.mention_tool_enabled = False
            plugin.mute_tool_enabled = False
            plugin.mute_duration_min = 1
            plugin.mute_duration_max = 10
            plugin.welcome_enabled = False
            plugin.welcome_message = "old"
            plugin.auto_accept_friend_request = False
            plugin.request_approval_enabled = False
            plugin.request_approval_qq = ""
            plugin.poke_stats_enabled = True
            plugin.poke_rank_max_limit = 100
            plugin.qzone_owner_qq = ""

            await plugin.update_console_settings({
                "self_mention_wakeup_enabled": False,
                "at_command_external_rules": ["ww帮助"],
                "keyword_wakeup_enabled": True,
                "keyword_wakeup_private_enabled": True,
                "keyword_wakeup_words": ["Airi", "小浅子"],
                "keyword_wakeup_mode": "prefix",
                "keyword_wakeup_group_whitelist": ["12345"],
                "keyword_wakeup_user_whitelist": ["54321"],
                "mention_tool_enabled": True,
                "mute_duration_min": 2,
                "mute_duration_max": 20,
                "welcome_enabled": True,
                "welcome_message": "欢迎！",
                "auto_accept_friend_request": True,
                "request_approval_qq": "123456",
                "poke_stats_enabled": False,
                "poke_rank_max_limit": 50,
                "qzone_auto_like_enabled": True,
                "qzone_dry_run": True,
                "qzone_poll_interval_sec": 180,
                "qzone_max_likes_per_cycle": 5,
                "qzone_friend_batch_size": 10,
                "qzone_feed_count": 40,
                "qzone_delay_min_sec": 20,
                "qzone_delay_max_sec": 35,
                "qzone_owner_qq": "123456",
            })

            self.assertTrue(plugin.config.saved)
            self.assertFalse(plugin.self_mention_wakeup_enabled)
            self.assertEqual(plugin.at_command_external_rules, ("ww帮助",))
            self.assertTrue(plugin.keyword_wakeup_enabled)
            self.assertTrue(plugin.keyword_wakeup_private_enabled)
            self.assertEqual(plugin.keyword_wakeup_words, ("airi", "小浅子"))
            self.assertEqual(plugin.keyword_wakeup_mode, "prefix")
            self.assertEqual(plugin.keyword_wakeup_group_whitelist, frozenset({"12345"}))
            self.assertEqual(plugin.keyword_wakeup_private_whitelist, frozenset({"54321"}))
            self.assertTrue(plugin.mention_tool_enabled)
            self.assertIn("mention_user", plugin.context.active)
            self.assertEqual((plugin.mute_duration_min, plugin.mute_duration_max), (2, 20))
            self.assertTrue(plugin.welcome_enabled)
            self.assertEqual(plugin.welcome_message, "欢迎！")
            self.assertTrue(plugin.auto_accept_friend_request)
            self.assertEqual(plugin.request_approval_qq, "123456")
            self.assertFalse(plugin.poke_stats_enabled)
            self.assertEqual(plugin.poke_rank_max_limit, 50)
            self.assertTrue(plugin._qzone_liker.enabled)
            self.assertEqual(plugin._qzone_liker.interval, 180)
            self.assertEqual(plugin._qzone_liker.max_likes, 5)
            self.assertEqual(plugin._qzone_liker.batch_size, 10)
            self.assertEqual(plugin._qzone_liker.feed_count, 40)
            self.assertEqual((plugin._qzone_liker.delay_min, plugin._qzone_liker.delay_max), (20, 35))

            await plugin.update_console_settings({"mention_tool_enabled": False, "qzone_auto_like_enabled": False})
            self.assertNotIn("mention_user", plugin.context.active)
            self.assertFalse(plugin._qzone_liker.enabled)
            self.assertGreaterEqual(plugin._qzone_liker.stopped, 1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
