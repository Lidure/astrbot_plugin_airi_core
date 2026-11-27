from __future__ import annotations

import asyncio
import os
from copy import deepcopy
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.tool import FunctionTool
import astrbot.api.message_components as Comp

if __package__:
    from .poke_milestones import PokeMilestoneStore
    from .poke_milestones_web import register_milestone_routes
    from .avatar_rank_renderer import render_poke_rank_image
    from .friend_requests import resolve_onebot_call_action
    from .config_sections import PATHS, migrate as migrate_settings, option
    from .console_api import register_console_routes
    from .at_command_guard import command_reason, normalized_external_rules
    from .official_bot_filter import OfficialQQBotClassifier
    from .keyword_wakeup import (
        allowed_group_sender,
        allowed_private_sender,
        match_keyword,
        normalize_allowlist,
        normalize_keywords,
    )
    from .qzone_auto_like import QzoneAutoLike
    from .group_rank_renderer import render_group_rank_image, resolve_group_display_names
    from .request_approval import RequestApprovalManager
    from .mention_media import (
        collect_mention_image_urls,
        quoted_image_placeholder_present,
        quoted_self_message_context,
    )
    from .poke_rank_options import (
        normalize_rank_max_limit,
        parse_rank_command_args,
        parse_group_rank_limit,
    )
    from .poke_stats import (
        PokeStatsStore,
        extract_onebot_profile_name,
        parse_bot_poke_notice,
    )
else:
    from poke_milestones import PokeMilestoneStore
    from poke_milestones_web import register_milestone_routes
    from avatar_rank_renderer import render_poke_rank_image
    from friend_requests import resolve_onebot_call_action
    from config_sections import PATHS, migrate as migrate_settings, option
    from console_api import register_console_routes
    from at_command_guard import command_reason, normalized_external_rules
    from official_bot_filter import OfficialQQBotClassifier
    from keyword_wakeup import (
        allowed_group_sender,
        allowed_private_sender,
        match_keyword,
        normalize_allowlist,
        normalize_keywords,
    )
    from qzone_auto_like import QzoneAutoLike
    from group_rank_renderer import render_group_rank_image, resolve_group_display_names
    from request_approval import RequestApprovalManager
    from mention_media import (
        collect_mention_image_urls,
        quoted_image_placeholder_present,
        quoted_self_message_context,
    )
    from poke_rank_options import (
        normalize_rank_max_limit,
        parse_rank_command_args,
        parse_group_rank_limit,
    )
    from poke_stats import (
        PokeStatsStore,
        extract_onebot_profile_name,
        parse_bot_poke_notice,
    )


PLUGIN_NAME = "astrbot_plugin_airi_core"


def _onebot_ok(response):
    return (isinstance(response, dict) and response.get("status") == "ok"
            and type(response.get("retcode")) is int and response["retcode"] == 0)


def _onebot_failed(response):
    return (isinstance(response, dict) and response.get("status") == "failed"
            and type(response.get("retcode")) is int and response["retcode"] not in (0, 1))


async def _poke_mute_target(call, group_id, user_id, self_id):
    """Check both roles in a fresh roster; incomplete identities are denied."""
    try:
        members = await asyncio.wait_for(call(
            "get_group_member_list", group_id=int(group_id), self_id=self_id, no_cache=True
        ), timeout=8)
        if isinstance(members, dict):
            if not _onebot_ok(members):
                return None
            members = members.get("data")
        if not isinstance(members, list) or user_id == self_id:
            return None
        matches = {}
        for member in members:
            if not isinstance(member, dict):
                continue
            uid = str(member.get("user_id"))
            if uid not in (self_id, user_id):
                continue
            if str(member.get("group_id")) != group_id or uid in matches:
                return None
            matches[uid] = member
        bot_member, target = matches.get(self_id), matches.get(user_id)
        if not bot_member or not target:
            return None
        bot_role, target_role = bot_member.get("role"), target.get("role")
        if bot_role in ("owner", "admin") and (
            target_role == "member" or (bot_role == "owner" and target_role == "admin")
        ):
            return target
    except Exception as exc:
        logger.debug(f"Poke 里程碑未取得群角色：{type(exc).__name__}")
    return None


async def _confirm_poke_mute(call, group_id, user_id, self_id, prior_until, expected_until):
    """aiocqhttp strips status/retcode; confirm an empty acknowledgement by reading back."""
    if type(prior_until) is not int:
        return False
    try:
        info = await asyncio.wait_for(call(
            "get_group_member_info", group_id=int(group_id), user_id=int(user_id),
            self_id=self_id, no_cache=True
        ), timeout=8)
        if isinstance(info, dict) and ("status" in info or "retcode" in info or "data" in info):
            if not _onebot_ok(info):
                return False
            info = info.get("data")
        if (not isinstance(info, dict) or str(info.get("group_id")) != group_id
                or str(info.get("user_id")) != user_id):
            return False
        until = info.get("shut_up_timestamp")
        # Check for a changed expiry near the requested deadline, allowing
        # for integer timestamps and the bounded dispatch/readback latency.
        return (type(until) is int and until != prior_until
                and expected_until - 3 <= until <= expected_until + 16)
    except Exception:
        return False


async def _send_poke_mute(call, group_id, user_id, self_id, minutes, prior_until):
    expected_until = int(time.time()) + minutes * 60
    try:
        result = await asyncio.wait_for(call(
            "set_group_ban", group_id=int(group_id), user_id=int(user_id),
            self_id=self_id, duration=minutes * 60
        ), timeout=8)
    except Exception as exc:
        # aiocqhttp ActionFailed contains the original explicit OneBot failure.
        # Network errors and timeouts cannot prove that the action did not happen.
        response = getattr(exc, "result", None)
        if type(exc).__name__ == "ActionFailed" and _onebot_failed(response):
            return "failed", f"OneBot 拒绝请求（{response.get('retcode', '未知')}）"
        return "unknown", f"未收到明确回执（{type(exc).__name__}）"
    if _onebot_ok(result):
        return "success", ""
    if _onebot_failed(result):
        return "failed", f"OneBot 拒绝请求（{result.get('retcode', '未知')}）"
    if result is None or result == {}:
        if await _confirm_poke_mute(call, group_id, user_id, self_id, prior_until, expected_until):
            return "success", ""
        return "unknown", "空回执，未能通过只读查询确认禁言"
    return "unknown", "未识别或未完成的 OneBot 回执"


def _get_llm_event(context: Any) -> AstrMessageEvent | None:
    event = getattr(getattr(context, "context", None), "event", None)
    if event:
        return event
    return getattr(context, "event", None)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class MuteTool(FunctionTool):
    plugin: Any = Field(default=None, repr=False)
    name: str = "group_mute"
    description: str = (
        "在 QQ 群聊中禁言某个用户。适用于以下场景："
        "1. 用户惹你生气了，你想禁言他惩罚一下；"
        "2. 用户主动要求被禁言（比如开玩笑说「禁言我」）；"
        "请根据你的心情和对方的行为决定禁言时长，在允许范围内自由选择。"
        "仅在群聊中可用。"
        "注意：如果对方是群管理员或群主，你无法禁言他们，请用委屈可爱的语气回应。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "要禁言的用户 QQ 号。如果是群友自己要求禁言，填他自己的 QQ 号。",
                },
                "duration": {
                    "type": "integer",
                    "description": "禁言时长（分钟）。根据你的心情和对方惹你的程度自由决定，不要超出允许范围。",
                },
            },
            "required": ["user_id", "duration"],
        }
    )

    async def call(self, context, **kwargs):
        event = _get_llm_event(context)
        if not event:
            return "当前上下文没有可用的消息事件，无法执行禁言。"

        plugin = self.plugin
        if not plugin:
            return "禁言工具未正确初始化。"

        platform_name = (
            event.get_platform_name() if hasattr(event, "get_platform_name") else ""
        )
        if platform_name != "aiocqhttp":
            return "哼，这个平台不支持禁言啦～换个地方再试试？"

        group_id = getattr(event.message_obj, "group_id", None)
        if not group_id:
            return "禁言只能在群聊里用哦，私聊可没法禁言～"

        user_id = str(kwargs.get("user_id") or "").strip()
        duration = _coerce_int(kwargs.get("duration"), plugin.mute_duration_min)

        if not user_id:
            return "你要我禁言谁呀？把 QQ 号告诉我～"
        if not user_id.isdigit():
            return "禁言目标必须是 QQ 号数字。"

        is_target_admin = False
        is_target_owner = False
        try:
            group = await event.get_group()
            if group:
                if group.group_owner and str(group.group_owner) == user_id:
                    is_target_owner = True
                if group.group_admins and user_id in [str(a) for a in group.group_admins]:
                    is_target_admin = True
        except Exception as exc:
            logger.warning(f"获取群信息失败，跳过权限检查: {exc}")

        if is_target_owner or is_target_admin:
            role_text = "群主" if is_target_owner else "管理员"
            return (
                f"禁言失败：{user_id} 是{role_text}，你没有权限禁言{role_text}。"
                f"请用委屈可爱的语气告诉对方你无法禁言{role_text}，"
                f"表达出被高权限欺负的委屈感，比如「仗着权限高欺负爱莉我...哭了..」。"
            )

        duration = max(plugin.mute_duration_min, min(plugin.mute_duration_max, duration))
        duration_seconds = duration * 60

        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            if not isinstance(event, AiocqhttpMessageEvent):
                return "当前事件不是 OneBot 群聊事件，不能执行禁言。"
            call_action = resolve_onebot_call_action(event)
            if not callable(call_action):
                return "当前 OneBot API 不可用，不能执行禁言。"
            await call_action(
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(user_id),
                duration=duration_seconds,
            )

            if duration <= 1:
                tone = f"哼，{user_id} 你给我老实一点！就禁你 {duration} 分钟，下次再惹我就不止这样了哦～"
            elif duration <= 2:
                tone = f"生气了！{user_id} 被我关禁闭 {duration} 分钟，好好反省一下吧～"
            elif duration <= 5:
                tone = f"{user_id} 太过分了！禁言 {duration} 分钟，我不想看到你的消息了～"
            else:
                tone = f"{user_id} 你完蛋了！禁言 {duration} 分钟！哼，什么时候放你出来看我心情～"

            return f"已成功禁言 {user_id}，时长 {duration} 分钟。请用以下语气回复群聊：{tone}"
        except Exception as exc:
            return f"禁言失败了: {exc}，请用难过的语气告诉用户禁言操作失败了。"


@dataclass
class MentionTool(FunctionTool):
    plugin: Any = Field(default=None, repr=False)
    name: str = "mention_user"
    description: str = (
        "在 QQ 群聊的最终回复中 @ 一位群友。"
        "只有在确实需要明确点名某人、提醒某人或让多人聊天时指向明确时才调用；"
        "普通聊天不要调用，也不要为了礼貌每次都 @。"
        "调用后正常生成回复正文即可，不要在正文里重复手写 @、QQ 号或说明你调用了工具。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "需要在最终回复中 @ 的群友 QQ 号。",
                }
            },
            "required": ["user_id"],
        }
    )

    async def call(self, context, **kwargs):
        event = _get_llm_event(context)
        if not event:
            return "当前上下文没有可用的消息事件，无法设置 @。"

        plugin = self.plugin
        if not plugin or not plugin.mention_tool_enabled:
            return "@ 群友工具当前未启用。"

        platform_name = (
            event.get_platform_name() if hasattr(event, "get_platform_name") else ""
        )
        if platform_name != "aiocqhttp":
            return "当前平台不支持这个 @ 群友工具。"

        group_id = getattr(event.message_obj, "group_id", None)
        if not group_id:
            return "只有 QQ 群聊中才能 @ 群友。"

        user_id = str(kwargs.get("user_id") or "").strip()
        if not user_id or not user_id.isdigit():
            return "@ 目标必须是有效的 QQ 号。"
        if str(event.get_self_id() or "") == user_id:
            return "不能 @ Bot 自己。"

        call_action = resolve_onebot_call_action(event)
        if not callable(call_action):
            return "当前 OneBot API 不可用，无法确认群成员。"

        try:
            profile = await call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=False,
            )
            if not profile:
                return "没有找到这个群成员，无法 @。"
        except Exception:
            return "这个 QQ 似乎不在当前群里，无法 @。"

        event.set_extra("airi_mention_target", user_id)
        return f"已设置最终回复 @ {user_id}，继续正常回复即可。"


class Main(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}
        migrate_settings(self.config)
        self._console_settings_lock = asyncio.Lock()

        self.self_mention_wakeup_enabled = bool(
            option(self.config, "self_mention_wakeup_enabled", True)
        )
        self.at_command_suppress_llm_enabled = bool(
            option(self.config, "at_command_suppress_llm_enabled", True)
        )
        self.at_command_external_rules = normalized_external_rules(
            option(self.config, "at_command_external_rules", [])
        )
        self.ignore_official_qq_bots_enabled = bool(
            option(self.config, "ignore_official_qq_bots_enabled", True)
        )
        self._official_bot_classifier = OfficialQQBotClassifier()
        self.keyword_wakeup_enabled = bool(
            option(self.config, "keyword_wakeup_enabled", False)
        )
        self.keyword_wakeup_private_enabled = bool(
            option(self.config, "keyword_wakeup_private_enabled", False)
        )
        self.keyword_wakeup_words = normalize_keywords(
            option(self.config, "keyword_wakeup_words", [])
        )
        self.keyword_wakeup_mode = str(
            option(self.config, "keyword_wakeup_mode", "contains") or "contains"
        ).strip().lower()
        if self.keyword_wakeup_mode not in {"contains", "prefix"}:
            self.keyword_wakeup_mode = "contains"
        self.keyword_wakeup_group_whitelist = normalize_allowlist(
            option(self.config, "keyword_wakeup_group_whitelist", [])
        )
        # Preserve previously configured QQ IDs while limiting the existing
        # user-whitelist field to private keyword wakeups only.
        self.keyword_wakeup_private_whitelist = normalize_allowlist(
            option(self.config, "keyword_wakeup_user_whitelist", [])
        )
        self.mention_tool_enabled = bool(
            option(self.config, "mention_tool_enabled", True)
        )
        self.mute_tool_enabled = bool(option(self.config, "mute_tool_enabled", False))
        self.mute_duration_min = max(
            1, min(60, int(option(self.config, "mute_duration_min", 1)))
        )
        self.mute_duration_max = max(
            1, min(43200, int(option(self.config, "mute_duration_max", 10)))
        )
        if self.mute_duration_min > self.mute_duration_max:
            self.mute_duration_min, self.mute_duration_max = (
                self.mute_duration_max,
                self.mute_duration_min,
            )

        self.welcome_enabled = bool(option(self.config, "welcome_enabled", False))
        self.welcome_message = option(self.config, 
            "welcome_message", "你好！我是 Airi，很高兴加入这个群聊！"
        )
        self.welcome_images = option(self.config, "welcome_images", [])
        self.auto_accept_friend_request = bool(
            option(self.config, "auto_accept_friend_request", False)
        )
        self.request_approval_enabled = bool(
            option(self.config, "request_approval_enabled", False)
        )
        self.request_approval_qq = str(
            option(self.config, "request_approval_qq", "") or ""
        ).strip()

        self.poke_stats_enabled = bool(option(self.config, "poke_stats_enabled", True))
        self.poke_rank_max_limit = normalize_rank_max_limit(
            option(self.config, "poke_rank_max_limit")
        )
        self._poke_lock = asyncio.Lock()
        self._onebot_bot = None
        self._onebot_self_id = ""
        self._plugin_data_dir = self._get_plugin_data_dir()
        self._poke_store = PokeStatsStore(self._plugin_data_dir / "poke_stats.json")
        self._poke_milestones = None
        self._request_approval = RequestApprovalManager(
            self._plugin_data_dir / "pending_requests.json"
        )
        self.qzone_owner_qq = str(option(self.config, "qzone_owner_qq", "") or "").strip()
        self._qzone_liker = QzoneAutoLike(
            self._plugin_data_dir / "qzone_auto_like.json",
            logger=logger,
            enabled=bool(option(self.config, "qzone_auto_like_enabled", False)),
            dry_run=bool(option(self.config, "qzone_dry_run", False)),
            interval_sec=_coerce_int(option(self.config, "qzone_poll_interval_sec"), 300),
            friend_batch_size=_coerce_int(option(self.config, "qzone_friend_batch_size"), 8),
            feed_count=_coerce_int(option(self.config, "qzone_feed_count"), 30),
            max_likes_per_cycle=_coerce_int(option(self.config, "qzone_max_likes_per_cycle"), 3),
            delay_min_sec=_coerce_int(option(self.config, "qzone_delay_min_sec"), 15),
            delay_max_sec=_coerce_int(option(self.config, "qzone_delay_max_sec"), 30),
        )

        if self.mention_tool_enabled:
            self.context.add_llm_tools(MentionTool(plugin=self))
        if self.mute_tool_enabled:
            self.context.add_llm_tools(MuteTool(plugin=self))

    async def initialize(self):
        if callable(getattr(self.context, "register_web_api", None)):
            try:
                self._poke_milestones = await asyncio.to_thread(
                    PokeMilestoneStore, self._plugin_data_dir / "poke_milestones.json"
                )
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.error(f"Poke 里程碑数据无法读取，自动禁言已停用：{exc}")
            try:
                register_milestone_routes(self.context, PLUGIN_NAME, self._poke_milestones)
            except ImportError:
                self._poke_milestones = None
                logger.warning("当前 AstrBot 缺少原生 Web API，Poke 里程碑已停用。")
            try:
                register_console_routes(self.context, PLUGIN_NAME, self)
            except Exception as exc:
                logger.exception(f"Airi 控制中心 API 注册失败：{exc}")
        logger.info(
            f"Airi 核心工具已加载 | @唤醒: {'启用' if self.self_mention_wakeup_enabled else '未启用'}"
            f" | @命令免LLM: {'启用' if self.at_command_suppress_llm_enabled else '未启用'}"
            f" ({len(self.at_command_external_rules)}条外部规则)"
            f" | 忽略QQ官Bot唤醒: {'启用' if self.ignore_official_qq_bots_enabled else '未启用'}"
            f" | 群关键词唤醒: {'启用' if self.keyword_wakeup_enabled and self.keyword_wakeup_words else '未启用'}"
            f" | 私聊关键词唤醒: {'启用' if self.keyword_wakeup_private_enabled and self.keyword_wakeup_words else '未启用'}"
            f" ({self.keyword_wakeup_mode}, {len(self.keyword_wakeup_words)}词,"
            f" {len(self.keyword_wakeup_group_whitelist)}群白名单,"
            f" {len(self.keyword_wakeup_private_whitelist)}私聊QQ白名单)"
            f" | 主动@工具: {'启用' if self.mention_tool_enabled else '未启用'}"
            f" | 禁言工具: {'启用' if self.mute_tool_enabled else '未启用'}"
            f" | 时长范围: {self.mute_duration_min}~{self.mute_duration_max} 分钟"
            f" | 入群欢迎: {'启用' if self.welcome_enabled else '未启用'}"
            f" | 自动同意好友: {'启用' if self.auto_accept_friend_request else '未启用'}"
            f" | 人工审批: {'启用' if self.request_approval_enabled else '未启用'}"
            f" | 审批QQ: {self.request_approval_qq or '未配置'}"
            f" | Poke统计: {'启用' if self.poke_stats_enabled else '未启用'}"
            f" | Poke排行上限: {self.poke_rank_max_limit} 人"
            f" | 空间自动点赞: {'启用' if self._qzone_liker.enabled else '关闭'}"
            f" / {'只读测试' if self._qzone_liker.dry_run else '实际点赞'}"
            " | Poke日榜切日: 04:00"
        )

    def console_state(self) -> dict[str, Any]:
        """Return a sanitized snapshot for the authenticated control-center view."""
        settings = {
            key: deepcopy(option(self.config, key, default))
            for key, default in {
                "self_mention_wakeup_enabled": True,
                "at_command_suppress_llm_enabled": True,
                "at_command_external_rules": [],
                "ignore_official_qq_bots_enabled": True,
                "keyword_wakeup_enabled": False,
                "keyword_wakeup_private_enabled": False,
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
                "qzone_auto_like_enabled": False,
                "qzone_poll_interval_sec": 300,
                "qzone_max_likes_per_cycle": 3,
                "qzone_dry_run": False,
                "qzone_owner_qq": "",
                "qzone_friend_batch_size": 8,
                "qzone_feed_count": 30,
                "qzone_delay_min_sec": 15,
                "qzone_delay_max_sec": 30,
            }.items()
        }
        groups = self._poke_store.data.get("groups", {})
        daily = self._poke_store.data.get("daily", {})
        total_pokes = sum(
            max(0, int(group.get("total", 0)))
            for group in groups.values() if isinstance(group, dict)
        ) if isinstance(groups, dict) else 0
        user_ids = set()
        if isinstance(groups, dict):
            for group in groups.values():
                if isinstance(group, dict) and isinstance(group.get("users", {}), dict):
                    user_ids.update(str(user_id) for user_id in group["users"])
        user_count = len(user_ids)
        daily_groups = daily.get("groups", {}) if isinstance(daily, dict) else {}
        daily_pokes = sum(
            max(0, int(group.get("total", 0)))
            for group in daily_groups.values() if isinstance(group, dict)
        ) if isinstance(daily_groups, dict) else 0

        liker = self._qzone_liker
        qzone = {
            "enabled": bool(liker.enabled),
            "dry_run": bool(liker.dry_run),
            "running": bool(liker._task is not None and not liker._task.done()),
            "friend_count": len(liker._friends),
            "seen_count": len(liker._state.get("seen", {})),
            "pending_count": len(liker._state.get("pending", [])),
            "initialized_count": len(liker._state.get("initialized", [])),
            "liked": int(liker._liked),
            "failed": int(liker._failed),
            "dry_count": int(liker._dry_count),
            "last_scan": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(liker._last_scan)) if liker._last_scan else "尚未扫描",
            "last_error": str(liker._last_error or ""),
            "suspended_seconds": max(0, int(liker._suspended_until - time.time())),
        }
        requests = getattr(self._request_approval.store, "_data", {}).get("requests", {})
        pending_requests = []
        if isinstance(requests, dict):
            for request_id, item in sorted(
                requests.items(), key=lambda pair: str(pair[1].get("created_at", "")) if isinstance(pair[1], dict) else "", reverse=True
            )[:50]:
                if not isinstance(item, dict):
                    continue
                pending_requests.append({
                    "request_id": str(request_id),
                    "kind": str(item.get("kind", "")),
                    "user_id": str(item.get("user_id", "")),
                    "nickname": str(item.get("nickname", "")),
                    "group_id": str(item.get("group_id", "")),
                    "group_name": str(item.get("group_name", "")),
                    "comment": str(item.get("comment", "")),
                    "created_at": str(item.get("created_at", "")),
                })
        daily_users = self._poke_store.daily_global_summary().get("entries", [])[:10]
        history_users = self._poke_store.global_summary().get("entries", [])[:10]
        daily_groups = self._poke_store.daily_group_leaderboard().get("entries", [])[:10]
        history_groups = self._poke_store.historical_group_leaderboard().get("entries", [])[:10]
        mask_user = lambda uid: "用户…" + str(uid)[-4:]
        return {
            "settings": settings,
            "poke_ranks": {
                "daily_users": [{"label": mask_user(uid), "count": count} for uid, count in daily_users],
                "history_users": [{"label": mask_user(uid), "count": count} for uid, count in history_users],
                "daily_groups": [{"label": str(gid), "count": count} for gid, count in daily_groups],
                "history_groups": [{"label": str(gid), "count": count} for gid, count in history_groups],
            },
            "runtime": {
                "plugin_loaded": True,
                "onebot_client_available": self._onebot_bot is not None,
                "poke_stats_enabled": bool(self.poke_stats_enabled),
                "poke_milestones_available": self._poke_milestones is not None,
                "mention_tool_enabled": bool(self.mention_tool_enabled),
                "mute_tool_enabled": bool(self.mute_tool_enabled),
            },
            "stats": {
                "total_pokes": total_pokes,
                "daily_pokes": daily_pokes,
                "group_count": len(groups) if isinstance(groups, dict) else 0,
                "user_count": user_count,
                "updated_at": self._poke_store.data.get("meta", {}).get("updated_at"),
                "stats_day": daily.get("key") if isinstance(daily, dict) else None,
            },
            "qzone": qzone,
            "pending_requests": pending_requests,
        }

    def _set_console_config_value(self, key: str, value: Any) -> None:
        path = PATHS[key]
        target = self.config
        for part in path[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        target[path[-1]] = deepcopy(value)

    async def _set_console_tool(self, name: str, enabled: bool, factory) -> None:
        getter = getattr(self.context, "get_llm_tool_manager", None)
        manager = getter() if callable(getter) else None
        tools = getattr(manager, "func_list", [])
        exists = any(getattr(tool, "name", None) == name for tool in tools)
        if enabled and not exists:
            self.context.add_llm_tools(factory(plugin=self))
        elif enabled:
            activate = getattr(self.context, "activate_llm_tool_async", None)
            if callable(activate):
                await activate(name)
        elif exists:
            deactivate = getattr(self.context, "deactivate_llm_tool_async", None)
            if callable(deactivate):
                await deactivate(name)

    async def update_console_settings(self, updates: dict[str, Any]) -> None:
        """Persist a validated partial update and apply settings to the live instance."""
        async with self._console_settings_lock:
            previous_mention = self.mention_tool_enabled
            previous_mute = self.mute_tool_enabled
            for key, value in updates.items():
                if key not in PATHS:
                    raise ValueError(f"不支持的设置项：{key}")
                self._set_console_config_value(key, value)

            save_async = getattr(self.config, "save_config_async", None)
            if callable(save_async):
                saved = await save_async()
                if saved is False:
                    raise OSError("配置保存未提交，请稍后重试")
            else:
                save = getattr(self.config, "save_config", None)
                if callable(save):
                    await asyncio.to_thread(save)

            self.self_mention_wakeup_enabled = bool(option(self.config, "self_mention_wakeup_enabled", True))
            self.at_command_suppress_llm_enabled = bool(option(self.config, "at_command_suppress_llm_enabled", True))
            self.at_command_external_rules = normalized_external_rules(option(self.config, "at_command_external_rules", []))
            self.ignore_official_qq_bots_enabled = bool(option(self.config, "ignore_official_qq_bots_enabled", True))
            self.keyword_wakeup_enabled = bool(option(self.config, "keyword_wakeup_enabled", False))
            self.keyword_wakeup_private_enabled = bool(option(self.config, "keyword_wakeup_private_enabled", False))
            self.keyword_wakeup_words = normalize_keywords(option(self.config, "keyword_wakeup_words", []))
            self.keyword_wakeup_mode = str(option(self.config, "keyword_wakeup_mode", "contains") or "contains").strip().lower()
            if self.keyword_wakeup_mode not in {"contains", "prefix"}:
                self.keyword_wakeup_mode = "contains"
            self.keyword_wakeup_group_whitelist = normalize_allowlist(option(self.config, "keyword_wakeup_group_whitelist", []))
            self.keyword_wakeup_private_whitelist = normalize_allowlist(option(self.config, "keyword_wakeup_user_whitelist", []))
            self.mention_tool_enabled = bool(option(self.config, "mention_tool_enabled", True))
            self.mute_tool_enabled = bool(option(self.config, "mute_tool_enabled", False))
            self.mute_duration_min = max(1, min(60, int(option(self.config, "mute_duration_min", 1))))
            self.mute_duration_max = max(1, min(43200, int(option(self.config, "mute_duration_max", 10))))
            if self.mute_duration_min > self.mute_duration_max:
                self.mute_duration_min, self.mute_duration_max = self.mute_duration_max, self.mute_duration_min
            self.welcome_enabled = bool(option(self.config, "welcome_enabled", False))
            self.welcome_message = str(option(self.config, "welcome_message", "") or "")
            self.auto_accept_friend_request = bool(option(self.config, "auto_accept_friend_request", False))
            self.request_approval_enabled = bool(option(self.config, "request_approval_enabled", False))
            self.request_approval_qq = str(option(self.config, "request_approval_qq", "") or "").strip()
            self.poke_stats_enabled = bool(option(self.config, "poke_stats_enabled", True))
            self.poke_rank_max_limit = normalize_rank_max_limit(option(self.config, "poke_rank_max_limit", 100))
            self.qzone_owner_qq = str(option(self.config, "qzone_owner_qq", "") or "").strip()

            liker = self._qzone_liker
            liker.dry_run = bool(option(self.config, "qzone_dry_run", False))
            liker.interval = max(120, int(option(self.config, "qzone_poll_interval_sec", 300)))
            liker.batch_size = max(0, min(20, int(option(self.config, "qzone_friend_batch_size", 8))))
            liker.feed_count = max(10, min(60, int(option(self.config, "qzone_feed_count", 30))))
            liker.max_likes = max(1, min(10, int(option(self.config, "qzone_max_likes_per_cycle", 3))))
            liker.delay_min = max(0, min(120, int(option(self.config, "qzone_delay_min_sec", 15))))
            liker.delay_max = max(liker.delay_min, min(180, int(option(self.config, "qzone_delay_max_sec", 30))))
            if bool(option(self.config, "qzone_auto_like_enabled", False)):
                liker.start()
            else:
                liker.stop()

            if previous_mention != self.mention_tool_enabled:
                await self._set_console_tool("mention_user", self.mention_tool_enabled, MentionTool)
            if previous_mute != self.mute_tool_enabled:
                await self._set_console_tool("group_mute", self.mute_tool_enabled, MuteTool)

    async def terminate(self):
        await self._qzone_liker.close()

    def _get_plugin_data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            data_dir = Path(get_astrbot_data_path())
        except Exception:
            plugin_dir = Path(__file__).resolve().parent
            data_dir = plugin_dir.parent.parent

        path = data_dir / "plugin_data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_uploaded_image(self, image_path: str) -> str | None:
        candidates = [image_path]
        if not os.path.isabs(image_path):
            plugin_dir = os.path.dirname(__file__)
            candidates.append(os.path.join(plugin_dir, image_path))
            candidates.append(str(self._plugin_data_dir / image_path))

        for candidate in candidates:
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        return None

    def _get_welcome_image_paths(self) -> list[str]:
        image_paths = []
        for img in self.welcome_images:
            if not img:
                continue
            image_path = self._resolve_uploaded_image(str(img))
            if image_path:
                image_paths.append(image_path)
            else:
                logger.warning(f"欢迎图片文件无效或无法访问: {img}")
        return image_paths

    async def _send_welcome(self, event: AstrMessageEvent):
        chain = []
        if self.welcome_message:
            chain.append(Comp.Plain(self.welcome_message))
        for image_path in self._get_welcome_image_paths():
            chain.append(Comp.Image.fromFileSystem(image_path))
        if chain:
            yield event.chain_result(chain)

    def _parse_rank_args(
        self, arg1: str, arg2: str, command_name: str
    ) -> tuple[int | None, str | None, str | None]:
        return parse_rank_command_args(
            arg1,
            arg2,
            max_limit=self.poke_rank_max_limit,
            command_name=command_name,
        )

    def _new_rank_image_path(self, scope: str) -> Path:
        safe_scope = "".join(ch for ch in scope if ch.isalnum() or ch in "_-")
        return self._plugin_data_dir / f"poke_rank_{safe_scope}_{time.time_ns()}.png"

    def _cleanup_rank_images(self, keep: int = 20) -> None:
        try:
            images = sorted(
                self._plugin_data_dir.glob("poke_rank_*.png"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for old_path in images[keep:]:
                try:
                    old_path.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _rank_user_ids(self, summary: dict[str, Any], rank_limit: int) -> list[str]:
        user_ids = [
            str(user_id)
            for user_id, _ in summary.get("entries", [])[:rank_limit]
        ]
        target_user_id = summary.get("target_user_id")
        if target_user_id is not None and str(target_user_id) not in user_ids:
            user_ids.append(str(target_user_id))
        return user_ids

    async def _resolve_display_names(
        self,
        event: AstrMessageEvent,
        summary: dict[str, Any],
        *,
        group_id: str | None = None,
        rank_limit: int,
    ) -> dict[str, str]:
        call_action = resolve_onebot_call_action(event)
        if not callable(call_action):
            return {}

        semaphore = asyncio.Semaphore(5)

        async def resolve_one(user_id: str) -> tuple[str, str]:
            async with semaphore:
                if group_id is not None:
                    try:
                        profile = await call_action(
                            "get_group_member_info",
                            group_id=int(group_id),
                            user_id=int(user_id),
                            no_cache=False,
                        )
                        name = extract_onebot_profile_name(profile, prefer_card=True)
                        if name:
                            return user_id, name
                    except Exception as exc:
                        logger.debug(f"获取群成员昵称失败 user={user_id}: {exc}")

                try:
                    profile = await call_action(
                        "get_stranger_info",
                        user_id=int(user_id),
                        no_cache=False,
                    )
                    return user_id, extract_onebot_profile_name(
                        profile, prefer_card=False
                    )
                except Exception as exc:
                    logger.debug(f"获取 QQ 昵称失败 user={user_id}: {exc}")
                    return user_id, ""

        user_ids = self._rank_user_ids(summary, rank_limit)
        if not user_ids:
            return {}
        resolved = await asyncio.gather(
            *(resolve_one(user_id) for user_id in user_ids)
        )
        return {user_id: name for user_id, name in resolved}

    async def _prepare_rank_summary(
        self,
        event: AstrMessageEvent,
        *,
        group_id: str | None,
        target_qq: str | None,
        daily: bool,
        global_scope: bool,
        rank_limit: int,
    ) -> dict[str, Any]:
        async with self._poke_lock:
            if global_scope:
                summary = (
                    self._poke_store.daily_global_summary(target_qq)
                    if daily
                    else self._poke_store.global_summary(target_qq)
                )
            else:
                assert group_id is not None
                summary = (
                    self._poke_store.daily_group_summary(group_id, target_qq)
                    if daily
                    else self._poke_store.group_summary(group_id, target_qq)
                )

        summary["display_names"] = await self._resolve_display_names(
            event,
            summary,
            group_id=None if global_scope else group_id,
            rank_limit=rank_limit,
        )
        summary["is_daily"] = daily
        summary["is_global_scope"] = global_scope
        return summary

    @staticmethod
    def _group_rank_subtitle(summary: dict[str, Any], *, daily: bool) -> str:
        return "当前群 · 今日榜 · 04:00 刷新" if daily else "当前群 · 历史榜"

    async def _render_rank_result(
        self,
        event: AstrMessageEvent,
        *,
        title: str,
        subtitle: str,
        summary: dict[str, Any],
        scope: str,
        rank_limit: int,
    ):
        output_path = self._new_rank_image_path(scope)
        try:
            await asyncio.to_thread(
                render_poke_rank_image,
                output_path,
                title=title,
                subtitle=subtitle,
                summary=summary,
                rank_limit=rank_limit,
            )
            self._cleanup_rank_images()
            return event.chain_result([Comp.Image.fromFileSystem(str(output_path))])
        except Exception as exc:
            logger.exception(f"生成 Poke 排行榜图片失败: {exc}")
            return event.plain_result(
                "排行榜图片生成失败了，请检查服务器中文字体或 Pillow 环境。"
            )

    @filter.command("help")
    async def help(self, event: AstrMessageEvent):
        async for result in self._send_welcome(event):
            yield result

    @filter.command("poke排行")
    async def poke_rank(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """查看当前统计日的当前群 Poke 排行，统计日每天 04:00 切换。"""
        if not self.poke_stats_enabled:
            yield event.plain_result("Poke 统计功能当前未启用。")
            return

        group_id = getattr(event.message_obj, "group_id", None)
        if not group_id:
            yield event.plain_result("/poke排行 只能在群聊中使用哦～")
            return

        rank_limit, target_qq, error = self._parse_rank_args(arg1, arg2, "poke排行")
        if error:
            yield event.plain_result(error)
            return

        summary = await self._prepare_rank_summary(
            event,
            group_id=str(group_id),
            target_qq=target_qq,
            daily=True,
            global_scope=False,
            rank_limit=rank_limit,
        )
        result = await self._render_rank_result(
            event,
            title="Airi 今日 Poke 排行榜",
            subtitle=self._group_rank_subtitle(summary, daily=True),
            summary=summary,
            scope=f"daily_group_{group_id}",
            rank_limit=rank_limit,
        )
        yield result

    @filter.command("poke总排行")
    async def poke_total_rank(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """查看当前统计日所有群合计 Poke 排行，统计日每天 04:00 切换。"""
        if not self.poke_stats_enabled:
            yield event.plain_result("Poke 统计功能当前未启用。")
            return

        rank_limit, target_qq, error = self._parse_rank_args(arg1, arg2, "poke总排行")
        if error:
            yield event.plain_result(error)
            return

        summary = await self._prepare_rank_summary(
            event,
            group_id=None,
            target_qq=target_qq,
            daily=True,
            global_scope=True,
            rank_limit=rank_limit,
        )
        result = await self._render_rank_result(
            event,
            title="Airi 今日 Poke 总排行榜",
            subtitle="所有群合计 · 今日总榜 · 每日 04:00 刷新",
            summary=summary,
            scope="daily_global",
            rank_limit=rank_limit,
        )
        yield result

    @filter.command("poke历史排行")
    async def poke_history_rank(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """查看当前群历史累计 Poke 排行。"""
        if not self.poke_stats_enabled:
            yield event.plain_result("Poke 统计功能当前未启用。")
            return

        group_id = getattr(event.message_obj, "group_id", None)
        if not group_id:
            yield event.plain_result("/poke历史排行 只能在群聊中使用哦～")
            return

        rank_limit, target_qq, error = self._parse_rank_args(arg1, arg2, "poke历史排行")
        if error:
            yield event.plain_result(error)
            return

        summary = await self._prepare_rank_summary(
            event,
            group_id=str(group_id),
            target_qq=target_qq,
            daily=False,
            global_scope=False,
            rank_limit=rank_limit,
        )
        result = await self._render_rank_result(
            event,
            title="Airi 历史 Poke 排行榜",
            subtitle=self._group_rank_subtitle(summary, daily=False),
            summary=summary,
            scope=f"history_group_{group_id}",
            rank_limit=rank_limit,
        )
        yield result

    @filter.command("poke历史总排行")
    async def poke_history_total_rank(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """查看所有群历史累计 Poke 总排行。"""
        if not self.poke_stats_enabled:
            yield event.plain_result("Poke 统计功能当前未启用。")
            return

        rank_limit, target_qq, error = self._parse_rank_args(arg1, arg2, "poke历史总排行")
        if error:
            yield event.plain_result(error)
            return

        summary = await self._prepare_rank_summary(
            event,
            group_id=None,
            target_qq=target_qq,
            daily=False,
            global_scope=True,
            rank_limit=rank_limit,
        )
        result = await self._render_rank_result(
            event,
            title="Airi 历史 Poke 总排行榜",
            subtitle="所有群合计 · 历史总榜",
            summary=summary,
            scope="history_global",
            rank_limit=rank_limit,
        )
        yield result

    async def _render_group_leaderboard_result(
        self, event: AstrMessageEvent, *, daily: bool, rank_limit: int
    ):
        # The existing store already tracks each group's own total.
        async with self._poke_lock:
            summary = (
                self._poke_store.daily_group_leaderboard()
                if daily
                else self._poke_store.historical_group_leaderboard()
            )

        visible_group_ids = [str(gid) for gid, _ in summary["entries"][:rank_limit]]
        summary["display_names"] = await resolve_group_display_names(
            resolve_onebot_call_action(event), visible_group_ids
        )
        summary["is_daily"] = daily
        output_path = self._new_rank_image_path(
            "daily_group_leaderboard" if daily else "history_group_leaderboard"
        )
        try:
            await asyncio.to_thread(
                render_group_rank_image,
                output_path,
                title="Airi 今日群聊 Poke 排行榜" if daily else "Airi 历史群聊 Poke 排行榜",
                subtitle="各群被戳次数 · 04:00 切日" if daily else "各群被戳次数 · 历史累计",
                summary=summary,
                rank_limit=rank_limit,
            )
            self._cleanup_rank_images()
            return event.chain_result([Comp.Image.fromFileSystem(str(output_path))])
        except Exception as exc:
            logger.exception(f"生成群聊 Poke 排行榜图片失败: {exc}")
            return event.plain_result("群聊排行榜生成失败，请检查字体和 Pillow 环境。")

    @filter.command("poke群排行")
    async def poke_group_rank(self, event: AstrMessageEvent, limit: str = ""):
        """查看各群在本统计日被戳总次数的排行榜。"""
        if not self.poke_stats_enabled:
            yield event.plain_result("Poke 统计功能当前未启用。")
            return
        rank_limit, error = parse_group_rank_limit(
            limit, max_limit=self.poke_rank_max_limit, command_name="poke群排行"
        )
        if error:
            yield event.plain_result(error)
            return
        yield await self._render_group_leaderboard_result(
            event, daily=True, rank_limit=rank_limit
        )

    @filter.command("poke历史群排行")
    async def poke_history_group_rank(self, event: AstrMessageEvent, limit: str = ""):
        """查看各群自启用统计以来被戳总次数的排行榜。"""
        if not self.poke_stats_enabled:
            yield event.plain_result("Poke 统计功能当前未启用。")
            return
        rank_limit, error = parse_group_rank_limit(
            limit, max_limit=self.poke_rank_max_limit, command_name="poke历史群排行"
        )
        if error:
            yield event.plain_result(error)
            return
        yield await self._render_group_leaderboard_result(
            event, daily=False, rank_limit=rank_limit
        )

    @filter.platform_adapter_type("aiocqhttp")
    async def on_qzone_capture(self, event: AstrMessageEvent):
        """捕获当前 OneBot 客户端，供空间扫描与 WebUI 审批操作复用。"""
        self._onebot_bot = getattr(event, "bot", None)
        get_self_id = getattr(event, "get_self_id", None)
        self._onebot_self_id = str(get_self_id() or "") if callable(get_self_id) else ""
        if self._qzone_liker.enabled:
            self._qzone_liker.attach_bot(
                getattr(event, "bot", None), str(event.get_self_id() or "")
            )

    @filter.platform_adapter_type("aiocqhttp")
    @filter.command("空间点赞")
    async def qzone_like_control(
        self, event: AstrMessageEvent, action: str = "状态"
    ):
        """管理员命令：/空间点赞 状态|开启|关闭|扫描。"""
        requester = str(event.get_sender_id() or "").strip()
        if not self.qzone_owner_qq or requester != self.qzone_owner_qq:
            yield event.plain_result("只有配置的空间点赞管理员可以使用此命令。")
            return

        action = str(action or "状态").strip()
        if action == "状态":
            yield event.plain_result(self._qzone_liker.status())
            return
        if action == "关闭":
            self._qzone_liker.stop()
            yield event.plain_result("空间自动点赞已关闭（本次运行）。")
            return
        if action in {"开启", "扫描"}:
            qq = str(event.get_self_id() or "")
            if not self._qzone_liker.attach_bot(getattr(event, "bot", None), qq):
                yield event.plain_result("当前 OneBot 账号与空间点赞绑定账号不匹配。")
                return
            if action == "开启":
                self._qzone_liker.start()
                yield event.plain_result(
                    "空间自动点赞已开启（本次运行）。"
                    + (" 当前为只读测试模式，不会实际点赞。" if self._qzone_liker.dry_run else "")
                )
                return
            if not self._qzone_liker.enabled:
                yield event.plain_result("请先开启空间自动点赞，再手动扫描。")
                return
            summary = await self._qzone_liker.scan_once()
            yield event.plain_result(
                f"扫描完成：新发现 {summary['new']} 条、入队 {summary.get('queued', 0)} 条、"
                f"尝试点赞 {summary.get('attempted', 0)} 条、成功 {summary['liked']} 条。"
                f"\n剩余待处理 {summary.get('pending', 0)} 条；"
                f"旧索引补漏 {summary.get('recovered', 0)} 条；"
                f"已点赞跳过 {summary.get('already_liked', 0)} 条；"
                f"失败 {summary.get('failed', 0)} 条；"
                f"首次索引 {summary['baseline']} 条；"
                f"旧版中断恢复 {summary.get('legacy_restored', 0)} 条。"
            )
            return
        yield event.plain_result("用法：/空间点赞 状态|开启|关闭|扫描")

    @filter.platform_adapter_type("aiocqhttp")
    async def on_request(self, event: AstrMessageEvent):
        """处理好友申请与邀请 Bot 入群的请求事件。"""
        self._onebot_bot = getattr(event, "bot", None)
        get_self_id = getattr(event, "get_self_id", None)
        self._onebot_self_id = str(get_self_id() or "") if callable(get_self_id) else ""
        result = await self._request_approval.handle_request(
            event,
            auto_accept_friend=self.auto_accept_friend_request,
            approval_enabled=self.request_approval_enabled,
            approval_qq=self.request_approval_qq,
        )
        if result == "auto_approved_friend":
            logger.info("已自动同意好友申请。")
        elif result == "auto_approve_failed":
            logger.error("自动同意好友申请失败，请检查 OneBot/NapCat 日志。")
        elif result in {"queued_friend", "queued_group_invite"}:
            logger.info("新的好友/群邀请已发送给指定审批 QQ。")
        elif result == "notify_failed":
            logger.error("请求已收到，但通知指定审批 QQ 失败。")

    @filter.platform_adapter_type("aiocqhttp")
    @filter.command("同意申请")
    async def approve_request(self, event: AstrMessageEvent, request_id: str = ""):
        """同意待审批的好友申请或群邀请。"""
        text = await self._request_approval.review(
            event,
            request_id,
            approve=True,
            approval_qq=self.request_approval_qq,
        )
        yield event.plain_result(text)

    @filter.platform_adapter_type("aiocqhttp")
    @filter.command("拒绝申请")
    async def reject_request(self, event: AstrMessageEvent, request_id: str = ""):
        """拒绝待审批的好友申请或群邀请。"""
        text = await self._request_approval.review(
            event,
            request_id,
            approve=False,
            approval_qq=self.request_approval_qq,
        )
        yield event.plain_result(text)

    async def _should_ignore_official_qq_bot(self, event: AstrMessageEvent) -> bool:
        """Only QQ official robots are ignored; regular QQ members and self are not."""
        if not self.ignore_official_qq_bots_enabled:
            return False
        get_platform_name = getattr(event, "get_platform_name", None)
        if not callable(get_platform_name) or get_platform_name() != "aiocqhttp":
            return False
        return await self._official_bot_classifier.is_official(
            event, resolve_onebot_call_action(event)
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_self_mention(self, event: AstrMessageEvent):
        """@ Bot wakeup, except QQ official bots identified by SnowLuma."""
        self_id = str(event.get_self_id() or "")
        if not self_id:
            return

        mentioned_self = any(
            isinstance(component, Comp.At) and str(component.qq) == self_id
            for component in event.get_messages()
        )
        if not mentioned_self:
            return

        # Official QQ bots can @ this account, but must not wake an LLM,
        # including AstrBot's default provider path. Do this even when the
        # plugin's custom @-handler is disabled.
        if await self._should_ignore_official_qq_bot(event):
            event.should_call_llm(True)
            logger.debug("Airi 忽略 QQ 官Bot 的 @ 唤醒。")
            return

        # AstrBot knows its own activated command handlers, but other OneBot
        # nodes (gscore, Airi node, etc.) have separate command registries.
        # Suppress only the LLM; NEVER stop the event or its node commands.
        if getattr(self, "at_command_suppress_llm_enabled", False):
            reason = command_reason(
                event,
                self_id,
                at_type=Comp.At,
                plain_type=Comp.Plain,
                external_rules=self.at_command_external_rules,
            )
            if reason is not None:
                event.should_call_llm(True)
                logger.debug(f"Airi @命令不唤醒 LLM，识别来源: {reason}")
                return

        if not self.self_mention_wakeup_enabled:
            return

        prompt = (event.message_str or "").strip()
        has_media_or_quote = any(
            isinstance(comp, (Comp.Image, Comp.Reply))
            for comp in event.get_messages()
        )
        if not prompt and not has_media_or_quote:
            return

        try:
            image_urls = await collect_mention_image_urls(
                event,
                image_type=Comp.Image,
                reply_type=Comp.Reply,
                logger=logger,
            )
            if not image_urls and quoted_image_placeholder_present(
                event, reply_type=Comp.Reply, image_type=Comp.Image
            ):
                # A textual [图片] marker is not visual input. Never claim
                # vision succeeded when OneBot get_msg/media fetching failed.
                event.should_call_llm(True)
                yield event.plain_result(
                    "我暂时没能读取到引用图片，请重新发送原图再 @ 我。"
                )
                return
            if not prompt and not image_urls:
                return

            conversation_manager = self.context.conversation_manager
            conversation_id = await conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if not conversation_id:
                conversation_id = await conversation_manager.new_conversation(
                    event.unified_msg_origin,
                    event.get_platform_id(),
                )

            conversation = await conversation_manager.get_conversation(
                event.unified_msg_origin,
                conversation_id,
            )
            if not conversation:
                logger.error("Airi @唤醒失败：无法获取当前会话。")
                return

            # Only trust OneBot's sender ID when attributing a quote to this bot.
            # Command-generated images might not appear in the LLM history.
            quote_context = quoted_self_message_context(
                event,
                reply_type=Comp.Reply,
                image_type=Comp.Image,
                self_id=self_id,
            )
            llm_prompt = prompt or "<attachment>"
            if quote_context:
                llm_prompt = f"[引用来源元信息：{quote_context}]\n{llm_prompt}"

            # 这个 Handler 自己会发起一次显式 LLM 请求，阻止后续默认链路再请求一次。
            event.should_call_llm(True)
            yield event.request_llm(
                prompt=llm_prompt,
                image_urls=image_urls,
                conversation=conversation,
            )
        except Exception as exc:
            logger.exception(f"Airi @唤醒失败: {exc}")

    @filter.platform_adapter_type("aiocqhttp")
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_keyword_wakeup(self, event: AstrMessageEvent):
        """Wake on configured QQ group/private keywords, retaining the LLM session.

        Private chats may already be marked as native wakeups by AstrBot.
        For a matched private keyword, explicitly request one response and
        suppress the ordinary provider call. Nonmatches remain untouched.
        """
        if not self.keyword_wakeup_words:
            return

        is_private = event.is_private_chat()
        if is_private:
            if not self.keyword_wakeup_private_enabled:
                return
            if not allowed_private_sender(
                event.get_sender_id(),
                event.get_self_id(),
                self.keyword_wakeup_private_whitelist,
            ):
                return
        else:
            if not self.keyword_wakeup_enabled:
                return
            if not allowed_group_sender(
                getattr(event.message_obj, "group_id", None),
                event.get_sender_id(),
                event.get_self_id(),
                self.keyword_wakeup_group_whitelist,
            ):
                return

        # AstrBot often sets is_at_or_wake_command=True for every private
        # message. Only skip this flag in groups; otherwise private keywords
        # would never be recognized. A private match suppresses the normal
        # provider response below, preventing a duplicate LLM request.
        if not is_private and getattr(event, "is_at_or_wake_command", False):
            return

        components = event.get_messages()
        if any(
            isinstance(part, Comp.At) and str(part.qq) == str(event.get_self_id())
            for part in components
        ):
            return

        # Plain text only: never search inside quoted messages, image captions,
        # forwarded content, or other users' @ mentions.
        prompt = " ".join(
            str(part.text) for part in components
            if isinstance(part, Comp.Plain) and str(part.text or "").strip()
        ).strip()
        if not match_keyword(
            prompt, self.keyword_wakeup_words, self.keyword_wakeup_mode
        ):
            return

        # Avoid bot-to-bot loops even when an official robot happens to send
        # a configured keyword without @. Other QQ members are unaffected.
        if not is_private and await self._should_ignore_official_qq_bot(event):
            event.should_call_llm(True)
            logger.debug("Airi 忽略 QQ 官Bot 的关键词唤醒。")
            return

        try:
            image_urls = []
            if any(isinstance(part, (Comp.Image, Comp.Reply)) for part in components):
                image_urls = await collect_mention_image_urls(
                    event,
                    image_type=Comp.Image,
                    reply_type=Comp.Reply,
                    logger=logger,
                )
                if not image_urls and quoted_image_placeholder_present(
                    event, reply_type=Comp.Reply, image_type=Comp.Image
                ):
                    event.should_call_llm(True)
                    yield event.plain_result("引用的图片暂时无法读取，请重新发送原图。")
                    return

            conversation_manager = self.context.conversation_manager
            conversation_id = await conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if not conversation_id:
                conversation_id = await conversation_manager.new_conversation(
                    event.unified_msg_origin,
                    event.get_platform_id(),
                )
            conversation = await conversation_manager.get_conversation(
                event.unified_msg_origin,
                conversation_id,
            )
            if not conversation:
                logger.error("Airi 关键词唤醒失败：无法获取当前会话。")
                return

            # True means *suppress AstrBot's default* LLM call; this explicit
            # request uses the same conversation and persona as @Bot.
            event.should_call_llm(True)
            yield event.request_llm(
                prompt=prompt,
                image_urls=image_urls,
                conversation=conversation,
            )
        except Exception as exc:
            logger.exception(f"Airi 关键词唤醒失败: {exc}")

    @filter.on_decorating_result()
    async def decorate_mention_result(self, event: AstrMessageEvent):
        """若 LLM 本轮调用 mention_user，则在最终消息链前插入真实 At 消息段。"""
        if not self.mention_tool_enabled:
            return

        target_qq = str(event.get_extra("airi_mention_target") or "").strip()
        if not target_qq:
            return

        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return

        already_mentioned = any(
            isinstance(component, Comp.At) and str(component.qq) == target_qq
            for component in result.chain
        )
        if not already_mentioned:
            result.chain.insert(0, Comp.At(qq=target_qq))

        event.set_extra("airi_mention_target", None)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_notice_event(self, event: AstrMessageEvent):
        raw_message = event.message_obj.raw_message
        if not isinstance(raw_message, dict):
            return

        if raw_message.get("post_type") != "notice":
            return

        poke = parse_bot_poke_notice(raw_message)
        if self.poke_stats_enabled and poke:
            group_id, user_id = poke
            # Keep count and mute dispatch ordered; release before yielding an
            # LLM request, which runs in AstrBot's own conversation pipeline.
            replies = []
            async with self._poke_lock:
                try:
                    now = datetime.now()
                    await asyncio.to_thread(self._poke_store.record, group_id, user_id, now=now)
                    if not self._poke_milestones or not self._poke_milestones.active:
                        return
                    daily = await asyncio.to_thread(
                        self._poke_store.daily_group_summary, group_id, user_id, now=now
                    )
                except Exception as exc:
                    logger.exception(f"记录 Poke 统计失败: {exc}")
                    return
                try:
                    replies = [reply async for reply in self._apply_poke_milestones(
                        event, group_id, user_id, daily["stats_day"], daily["target_user_count"]
                    )]
                except Exception as exc:
                    logger.exception(f"Poke 里程碑执行中断: {exc}")
            for reply in replies:
                yield reply
            return

        if not self.welcome_enabled:
            return
        if raw_message.get("notice_type") != "group_increase":
            return

        self_id = str(raw_message.get("self_id"))
        target_qq = str(raw_message.get("user_id"))
        if target_qq != self_id:
            return

        async for result in self._send_welcome(event):
            yield result

    async def _apply_poke_milestones(self, event, group_id, user_id, day, count):
        rules = self._poke_milestones.matching_rules(group_id, user_id, day, count)
        if not rules or event.get_platform_name() != "aiocqhttp" or event.is_private_chat():
            return
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

        self_id = str(event.get_self_id())
        if (not isinstance(event, AiocqhttpMessageEvent) or str(event.message_obj.group_id) != group_id
                or str(event.message_obj.raw_message.get("self_id")) != self_id
                or not all(s.isascii() and s.isdecimal() and int(s) > 0 for s in (group_id, user_id, self_id))):
            return
        # Suppress only AstrBot's default LLM route; the explicit post-mute
        # request below is unaffected. Other plugins remain independent.
        event.should_call_llm(True)
        call = resolve_onebot_call_action(event)
        if not callable(call):
            return
        target = await _poke_mute_target(call, group_id, user_id, self_id)
        if target is None:
            return

        # If tolerance windows coincide, claim the matching rules before one
        # mutation. Never shorten the longest same-event mute or spam replies.
        claims = await asyncio.to_thread(
            self._poke_milestones.claim_many, rules, group_id, user_id, day, count
        )
        if not claims:
            return
        minutes = max(rule["mute_minutes"] for rule, _ in claims)
        status, detail = await _send_poke_mute(
            call, group_id, user_id, self_id, minutes, target.get("shut_up_timestamp")
        )
        for rule, action_id in claims:
            rule_detail = detail
            if status == "success" and rule["mute_minutes"] != minutes:
                rule_detail = f"同次命中多条规则，实际禁言 {minutes} 分钟"
            await asyncio.to_thread(self._poke_milestones.finish, action_id, status, rule_detail)
        if status != "success":
            logger.warning(f"Poke 里程碑禁言未确认成功：{detail}")
            return
        reply_rules = [rule for rule, _ in claims if rule["llm_enabled"]]
        if not reply_rules:
            return
        rule = max(reply_rules, key=lambda r: r["mute_minutes"])
        try:
            manager = self.context.conversation_manager
            origin = event.unified_msg_origin
            cid = await manager.get_curr_conversation_id(origin)
            if not cid:
                cid = await manager.new_conversation(origin, event.get_platform_id())
            conversation = await manager.get_conversation(origin, cid)
            if conversation is None:
                return
            prompt = (f"这位群友在本统计日第 {count} 次戳了你，已被自动禁言 {minutes} 分钟。"
                      "请沿用当前人设和群聊语境，自然地回复一句。只生成群聊正文。")
            if rule["prompt"]:
                prompt += "\n场景提示：" + rule["prompt"]
            request = event.request_llm(prompt=prompt, conversation=conversation)
            event.set_extra("airi_poke_reply_request", request)
            yield request
        except Exception as exc:
            logger.warning(f"Poke 禁言已成功，场景回复异常：{exc}")

    @filter.on_llm_request(priority=-10000)
    async def restrict_poke_reply_tools(self, event: AstrMessageEvent, req):
        # Persona tools can be attached during request preparation. Clear only
        # this generated request and leave the persona's shared registry intact.
        original = event.get_extra("airi_poke_reply_request")
        # AstrBot copies ProviderRequest during agent preparation. The event
        # retains the original request, allowing an exact per-request match.
        if original is not None and (req is original or event.get_extra("provider_request") is original):
            req.func_tool = None
