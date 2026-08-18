from __future__ import annotations

import os
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.tool import FunctionTool
import astrbot.api.message_components as Comp


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

        # ---- 检测目标用户是否为管理员/群主 ----
        is_target_admin = False
        is_target_owner = False
        try:
            group = await event.get_group()
            if group:
                if group.group_owner and str(group.group_owner) == user_id:
                    is_target_owner = True
                if group.group_admins and user_id in [
                    str(a) for a in group.group_admins
                ]:
                    is_target_admin = True
        except Exception as e:
            logger.warning(f"获取群信息失败，跳过权限检查: {e}")

        if is_target_owner or is_target_admin:
            role_text = "群主" if is_target_owner else "管理员"
            return (
                f"禁言失败：{user_id} 是{role_text}，你没有权限禁言{role_text}。"
                f"请用委屈可爱的语气告诉对方你无法禁言{role_text}，"
                f"表达出被高权限欺负的委屈感，比如「仗着权限高欺负爱莉我...哭了..」。"
            )

        # ---- 执行禁言 ----
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

            # 构建返回信息，LLM 将根据此内容生成回复
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
        self.mute_duration_min = max(1, min(60, int(self.config.get("mute_duration_min", 1))))
        self.mute_duration_max = max(1, min(43200, int(self.config.get("mute_duration_max", 10))))
        if self.mute_duration_min > self.mute_duration_max:
            self.mute_duration_min, self.mute_duration_max = self.mute_duration_max, self.mute_duration_min

        # 欢迎消息配置
        self.welcome_enabled = bool(self.config.get("welcome_enabled", False))
        self.welcome_message = self.config.get("welcome_message", "你好！我是 Airi，很高兴加入这个群聊！")
        self.welcome_images = self.config.get("welcome_images", [])

        if self.mute_tool_enabled:
            self.context.add_llm_tools(MuteTool(plugin=self))

    async def initialize(self):
        logger.info(
            f"Airi 核心工具已加载 | 禁言工具: {'启用' if self.mute_tool_enabled else '未启用'}"
            f" | 时长范围: {self.mute_duration_min}~{self.mute_duration_max} 分钟"
            f" | 入群欢迎: {'启用' if self.welcome_enabled else '未启用'}"
        )

    async def terminate(self):
        pass

    def _resolve_uploaded_image(self, image_path: str) -> str | None:
        """解析 AstrBot 上传配置返回的绝对或相对文件路径。"""
        candidates = [image_path]
        if not os.path.isabs(image_path):
            plugin_dir = os.path.dirname(__file__)
            candidates.append(os.path.join(plugin_dir, image_path))

            # AstrBot 的 file 配置通常返回 files/...，实际文件位于
            # data/plugin_data/<plugin_name>/ 下，而不是插件源码目录。
            data_dir = os.path.dirname(os.path.dirname(plugin_dir))
            candidates.append(
                os.path.join(data_dir, "plugin_data", PLUGIN_NAME, image_path)
            )

        for candidate in candidates:
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        return None

    def _get_welcome_image_paths(self) -> list[str]:
        """获取欢迎图片的绝对路径。"""
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
        """将欢迎文字和图片组合成一条消息发送。"""
        chain = []
        if self.welcome_message:
            chain.append(Comp.Plain(self.welcome_message))

        for image_path in self._get_welcome_image_paths():
            chain.append(Comp.Image.fromFileSystem(image_path))

        if chain:
            yield event.chain_result(chain)

    @filter.command("help")
    async def help(self, event: AstrMessageEvent):
        """发送帮助内容（复用配置的欢迎消息和欢迎图片）。"""
        async for result in self._send_welcome(event):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_member_change(self, event: AstrMessageEvent):
        if not self.welcome_enabled:
            return

        raw_message = event.message_obj.raw_message
        if not isinstance(raw_message, dict) or raw_message.get("post_type") != "notice":
            return

        notice_type = raw_message.get("notice_type")
        if notice_type != "group_increase":
            return

        self_id = str(raw_message.get("self_id"))
        target_qq = str(raw_message.get("user_id"))
        if target_qq != self_id:
            return

        group_id = str(raw_message.get("group_id"))

        async for result in self._send_welcome(event):
            yield result
