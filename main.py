from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.tool import FunctionTool


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

        platform_name = event.get_platform_name() if hasattr(event, "get_platform_name") else ""
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

        duration = max(plugin.mute_duration_min, min(plugin.mute_duration_max, duration))
        duration_seconds = duration * 60

        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
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
                return f"哼，{user_id} 你给我老实一点！就禁你 {duration} 分钟，下次再惹我就不止这样了哦～"
            elif duration <= 2:
                return f"生气了！{user_id} 被我关禁闭 {duration} 分钟，好好反省一下吧～"
            elif duration <= 5:
                return f"{user_id} 太过分了！禁言 {duration} 分钟，我不想看到你的消息了～"
            else:
                return f"{user_id} 你完蛋了！禁言 {duration} 分钟！哼，什么时候放你出来看我心情～"
        except Exception as exc:
            return f"禁言失败了呜呜... {exc}"


class Main(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.config = config or {}

        self.mute_tool_enabled = bool(self.config.get("mute_tool_enabled", False))
        self.mute_duration_min = max(1, min(60, int(self.config.get("mute_duration_min", 1))))
        self.mute_duration_max = max(1, min(43200, int(self.config.get("mute_duration_max", 10))))
        if self.mute_duration_min > self.mute_duration_max:
            self.mute_duration_min, self.mute_duration_max = self.mute_duration_max, self.mute_duration_min

        if self.mute_tool_enabled:
            self.context.add_llm_tools(MuteTool(plugin=self))

    async def initialize(self):
        logger.info(
            f"Airi 核心工具已加载 | 禁言工具: {'启用' if self.mute_tool_enabled else '未启用'}"
            f" | 时长范围: {self.mute_duration_min}~{self.mute_duration_max} 分钟"
        )

    async def terminate(self):
        pass
