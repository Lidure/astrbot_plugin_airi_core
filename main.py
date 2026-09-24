from __future__ import annotations

import asyncio
import os
import time
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
    from .avatar_rank_renderer import render_poke_rank_image
    from .friend_requests import maybe_accept_friend_request
    from .poke_stats import (
        PokeStatsStore,
        extract_onebot_profile_name,
        parse_bot_poke_notice,
    )
else:
    from avatar_rank_renderer import render_poke_rank_image
    from friend_requests import maybe_accept_friend_request
    from poke_stats import (
        PokeStatsStore,
        extract_onebot_profile_name,
        parse_bot_poke_notice,
    )


PLUGIN_NAME = "astrbot_plugin_airi_core"


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
            client = event.bot
            await client.api.call_action(
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


class Main(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.config = config or {}

        self.mute_tool_enabled = bool(self.config.get("mute_tool_enabled", False))
        self.mute_duration_min = max(
            1, min(60, int(self.config.get("mute_duration_min", 1)))
        )
        self.mute_duration_max = max(
            1, min(43200, int(self.config.get("mute_duration_max", 10)))
        )
        if self.mute_duration_min > self.mute_duration_max:
            self.mute_duration_min, self.mute_duration_max = (
                self.mute_duration_max,
                self.mute_duration_min,
            )

        self.welcome_enabled = bool(self.config.get("welcome_enabled", False))
        self.welcome_message = self.config.get(
            "welcome_message", "你好！我是 Airi，很高兴加入这个群聊！"
        )
        self.welcome_images = self.config.get("welcome_images", [])
        self.auto_accept_friend_request = bool(
            self.config.get("auto_accept_friend_request", False)
        )

        self.poke_stats_enabled = bool(self.config.get("poke_stats_enabled", True))
        self.poke_rank_limit = max(
            3, min(30, int(self.config.get("poke_rank_limit", 10)))
        )
        self._poke_lock = asyncio.Lock()
        self._plugin_data_dir = self._get_plugin_data_dir()
        self._poke_store = PokeStatsStore(self._plugin_data_dir / "poke_stats.json")

        if self.mute_tool_enabled:
            self.context.add_llm_tools(MuteTool(plugin=self))

    async def initialize(self):
        logger.info(
            f"Airi 核心工具已加载 | 禁言工具: {'启用' if self.mute_tool_enabled else '未启用'}"
            f" | 时长范围: {self.mute_duration_min}~{self.mute_duration_max} 分钟"
            f" | 入群欢迎: {'启用' if self.welcome_enabled else '未启用'}"
            f" | 自动同意好友: {'启用' if self.auto_accept_friend_request else '未启用'}"
            f" | Poke统计: {'启用' if self.poke_stats_enabled else '未启用'}"
            " | Poke日榜切日: 04:00"
        )

    async def terminate(self):
        pass

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

    @staticmethod
    def _validate_qq_arg(qq: str, command_name: str) -> tuple[str | None, str | None]:
        qq = str(qq or "").strip()
        if not qq:
            return None, None
        if not qq.isdigit():
            return None, f"QQ 号必须是纯数字，例如：/{command_name} 123456789"
        return qq, None

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

    def _rank_user_ids(self, summary: dict[str, Any]) -> list[str]:
        user_ids = [
            str(user_id)
            for user_id, _ in summary.get("entries", [])[: self.poke_rank_limit]
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
    ) -> dict[str, str]:
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
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

        user_ids = self._rank_user_ids(summary)
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
    ):
        output_path = self._new_rank_image_path(scope)
        try:
            await asyncio.to_thread(
                render_poke_rank_image,
                output_path,
                title=title,
                subtitle=subtitle,
                summary=summary,
                rank_limit=self.poke_rank_limit,
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
    async def poke_rank(self, event: AstrMessageEvent, qq: str = ""):
        """查看当前统计日的当前群 Poke 排行，统计日每天 04:00 切换。"""
        if not self.poke_stats_enabled:
            yield event.plain_result("Poke 统计功能当前未启用。")
            return

        group_id = getattr(event.message_obj, "group_id", None)
        if not group_id:
            yield event.plain_result("/poke排行 只能在群聊中使用哦～")
            return

        target_qq, error = self._validate_qq_arg(qq, "poke排行")
        if error:
            yield event.plain_result(error)
            return

        summary = await self._prepare_rank_summary(
            event,
            group_id=str(group_id),
            target_qq=target_qq,
            daily=True,
            global_scope=False,
        )
        result = await self._render_rank_result(
            event,
            title="Airi 今日 Poke 排行榜",
            subtitle=self._group_rank_subtitle(summary, daily=True),
            summary=summary,
            scope=f"daily_group_{group_id}",
        )
        yield result

    @filter.command("poke总排行")
    async def poke_total_rank(self, event: AstrMessageEvent, qq: str = ""):
        """查看当前统计日所有群合计 Poke 排行，统计日每天 04:00 切换。"""
        if not self.poke_stats_enabled:
            yield event.plain_result("Poke 统计功能当前未启用。")
            return

        target_qq, error = self._validate_qq_arg(qq, "poke总排行")
        if error:
            yield event.plain_result(error)
            return

        summary = await self._prepare_rank_summary(
            event,
            group_id=None,
            target_qq=target_qq,
            daily=True,
            global_scope=True,
        )
        result = await self._render_rank_result(
            event,
            title="Airi 今日 Poke 总排行榜",
            subtitle="所有群合计 · 今日总榜 · 每日 04:00 刷新",
            summary=summary,
            scope="daily_global",
        )
        yield result

    @filter.command("poke历史排行")
    async def poke_history_rank(self, event: AstrMessageEvent, qq: str = ""):
        """查看当前群历史累计 Poke 排行。"""
        if not self.poke_stats_enabled:
            yield event.plain_result("Poke 统计功能当前未启用。")
            return

        group_id = getattr(event.message_obj, "group_id", None)
        if not group_id:
            yield event.plain_result("/poke历史排行 只能在群聊中使用哦～")
            return

        target_qq, error = self._validate_qq_arg(qq, "poke历史排行")
        if error:
            yield event.plain_result(error)
            return

        summary = await self._prepare_rank_summary(
            event,
            group_id=str(group_id),
            target_qq=target_qq,
            daily=False,
            global_scope=False,
        )
        result = await self._render_rank_result(
            event,
            title="Airi 历史 Poke 排行榜",
            subtitle=self._group_rank_subtitle(summary, daily=False),
            summary=summary,
            scope=f"history_group_{group_id}",
        )
        yield result

    @filter.command("poke历史总排行")
    async def poke_history_total_rank(self, event: AstrMessageEvent, qq: str = ""):
        """查看所有群历史累计 Poke 总排行。"""
        if not self.poke_stats_enabled:
            yield event.plain_result("Poke 统计功能当前未启用。")
            return

        target_qq, error = self._validate_qq_arg(qq, "poke历史总排行")
        if error:
            yield event.plain_result(error)
            return

        summary = await self._prepare_rank_summary(
            event,
            group_id=None,
            target_qq=target_qq,
            daily=False,
            global_scope=True,
        )
        result = await self._render_rank_result(
            event,
            title="Airi 历史 Poke 总排行榜",
            subtitle="所有群合计 · 历史总榜",
            summary=summary,
            scope="history_global",
        )
        yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_notice_event(self, event: AstrMessageEvent):
        raw_message = event.message_obj.raw_message
        if not isinstance(raw_message, dict):
            return

        if raw_message.get("post_type") == "request":
            if not self.auto_accept_friend_request:
                return

            bot = getattr(event, "bot", None)
            api = getattr(bot, "api", None)
            call_action = getattr(api, "call_action", None)
            if not callable(call_action):
                if raw_message.get("request_type") == "friend":
                    logger.warning("收到好友申请，但当前 OneBot API 不可用，无法自动同意。")
                return

            result = await maybe_accept_friend_request(
                raw_message,
                call_action,
                enabled=True,
            )
            if result.matched:
                if result.approved:
                    logger.info(f"已自动同意好友申请: user={result.user_id or 'unknown'}")
                else:
                    logger.error(
                        f"自动同意好友申请失败: user={result.user_id or 'unknown'}, "
                        f"error={result.error or 'unknown'}"
                    )
            return

        if raw_message.get("post_type") != "notice":
            return

        poke = parse_bot_poke_notice(raw_message)
        if self.poke_stats_enabled and poke:
            group_id, user_id = poke
            try:
                async with self._poke_lock:
                    await asyncio.to_thread(self._poke_store.record, group_id, user_id)
            except Exception as exc:
                logger.exception(f"记录 Poke 统计失败: {exc}")
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
