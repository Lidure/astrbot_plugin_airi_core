"""Grouped Airi Core configuration with a one-time migration of flat plugin settings.

AstrBot stores `type: object` groups as nested dicts. Retain invisible legacy
schema entries for one release to copy previously customized values safely.
"""
from __future__ import annotations

from typing import Any

PATHS = {
    "self_mention_wakeup_enabled": ("chat_settings","self_mention_wakeup_enabled"),
    "keyword_wakeup_enabled": ("chat_settings", "keyword_wakeup_enabled"),
    "keyword_wakeup_words": ("chat_settings", "keyword_wakeup_words"),
    "keyword_wakeup_mode": ("chat_settings", "keyword_wakeup_mode"),
    "keyword_wakeup_group_whitelist": ("chat_settings", "keyword_wakeup_group_whitelist"),
    "keyword_wakeup_user_whitelist": ("chat_settings", "keyword_wakeup_user_whitelist"),
    "mention_tool_enabled": ("chat_settings","mention_tool_enabled"),
    "mute_tool_enabled": ("moderation_settings","mute_tool_enabled"),
    "mute_duration_min": ("moderation_settings","mute_duration_min"),
    "mute_duration_max": ("moderation_settings","mute_duration_max"),
    "welcome_enabled": ("welcome_settings","welcome_enabled"),
    "welcome_message": ("welcome_settings","welcome_message"),
    "welcome_images": ("welcome_settings","welcome_images"),
    "auto_accept_friend_request": ("request_settings","auto_accept_friend_request"),
    "request_approval_enabled": ("request_settings","request_approval_enabled"),
    "request_approval_qq": ("request_settings","request_approval_qq"),
    "poke_stats_enabled": ("poke_settings","poke_stats_enabled"),
    "poke_rank_max_limit": ("poke_settings","poke_rank_max_limit"),
    "profile_like_auto_reply_enabled": ("profile_like_settings","profile_like_auto_reply_enabled"),
    "qzone_auto_like_enabled": ("qzone_settings","qzone_auto_like_enabled"),
    "qzone_poll_interval_sec": ("qzone_settings","qzone_poll_interval_sec"),
    "qzone_max_likes_per_cycle": ("qzone_settings","qzone_max_likes_per_cycle"),
    "qzone_dry_run": ("qzone_settings","advanced","qzone_dry_run"),
    "qzone_owner_qq": ("qzone_settings","advanced","qzone_owner_qq"),
    "qzone_friend_batch_size": ("qzone_settings","advanced","qzone_friend_batch_size"),
    "qzone_feed_count": ("qzone_settings","advanced","qzone_feed_count"),
    "qzone_delay_min_sec": ("qzone_settings","advanced","qzone_delay_min_sec"),
    "qzone_delay_max_sec": ("qzone_settings","advanced","qzone_delay_max_sec"),
}
NEW_DEFAULTS = {
    "self_mention_wakeup_enabled": True,
    "keyword_wakeup_enabled": False,
    "keyword_wakeup_words": [],
    "keyword_wakeup_mode": "contains",
    "keyword_wakeup_group_whitelist": [],
    "keyword_wakeup_user_whitelist": [],
    "mention_tool_enabled": True,
    "mute_tool_enabled": False,
    "mute_duration_min": 1,
    "mute_duration_max": 10,
    "welcome_enabled": False,
    "welcome_message": "你好！我是 Airi，很高兴加入这个群聊！",
    "welcome_images": [],
    "auto_accept_friend_request": False,
    "request_approval_enabled": False,
    "request_approval_qq": "",
    "poke_stats_enabled": True,
    "poke_rank_max_limit": 100,
    "profile_like_auto_reply_enabled": False,
    "qzone_auto_like_enabled": False,
    "qzone_poll_interval_sec": 300,
    "qzone_max_likes_per_cycle": 3,
    "qzone_dry_run": False,
    "qzone_owner_qq": "",
    "qzone_friend_batch_size": 8,
    "qzone_feed_count": 30,
    "qzone_delay_min_sec": 15,
    "qzone_delay_max_sec": 30,
}
LEGACY_DEFAULTS = {
    "self_mention_wakeup_enabled": True,
    "keyword_wakeup_enabled": False,
    "keyword_wakeup_words": [],
    "keyword_wakeup_mode": "contains",
    "keyword_wakeup_group_whitelist": [],
    "keyword_wakeup_user_whitelist": [],
    "mention_tool_enabled": True,
    "mute_tool_enabled": False,
    "mute_duration_min": 1,
    "mute_duration_max": 10,
    "welcome_enabled": False,
    "welcome_message": "你好！我是 Airi，很高兴加入这个群聊！",
    "welcome_images": [],
    "auto_accept_friend_request": False,
    "request_approval_enabled": False,
    "request_approval_qq": "",
    "poke_stats_enabled": True,
    "poke_rank_max_limit": 100,
    "profile_like_auto_reply_enabled": False,
    "qzone_auto_like_enabled": False,
    "qzone_poll_interval_sec": 300,
    "qzone_max_likes_per_cycle": 3,
    "qzone_dry_run": True,
    "qzone_owner_qq": "",
    "qzone_friend_batch_size": 8,
    "qzone_feed_count": 30,
    "qzone_delay_min_sec": 15,
    "qzone_delay_max_sec": 30,
}
MIGRATION_KEY = "airi_group_settings_migrated"


def _section(config: dict, path: tuple[str, ...]) -> dict:
    current = config
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    return current


def option(config: dict, key: str, default: Any = None) -> Any:
    """Read grouped configuration, falling back to legacy top-level fields."""
    path = PATHS.get(key)
    if not path:
        return config.get(key, default)
    current: Any = config
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return config.get(key, default)
        current = current[part]
    return current


def migrate(config: dict) -> bool:
    """Migrate old nondefault values once, with a save for surviving restarts.

    Existing grouped values different from their defaults are never replaced.
    An older, enabled Qzone installation's dry-run=true is preserved; in new
    installs the new default is false so the feature has a single master switch.
    """
    if config.get(MIGRATION_KEY):
        return False

    for key, path in PATHS.items():
        target = _section(config, path)
        name = path[-1]
        target.setdefault(name, NEW_DEFAULTS[key])
        if key not in config:
            continue

        previous = config[key]
        changed_from_old_default = previous != LEGACY_DEFAULTS[key]
        if key == "qzone_dry_run" and config.get("qzone_auto_like_enabled", False):
            changed_from_old_default = True
        if changed_from_old_default and target[name] == NEW_DEFAULTS[key]:
            target[name] = previous

    config[MIGRATION_KEY] = True
    save = getattr(config, "save_config", None)
    if callable(save):
        try:
            save()
        except Exception:
            # Not every AstrBot/test configuration wrapper has a writable
            # backing file; runtime still uses the migrated in-memory values.
            pass
    return True
