"""Authenticated API endpoints for the always-visible Airi control center."""
from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from typing import Any

if __package__:
    from .config_sections import PATHS
else:
    from config_sections import PATHS


EDITABLE_KEYS = frozenset(PATHS) - {"welcome_images"}
BOOL_KEYS = frozenset({
    "self_mention_wakeup_enabled", "at_command_suppress_llm_enabled",
    "ignore_official_qq_bots_enabled", "keyword_wakeup_enabled",
    "keyword_wakeup_private_enabled", "mention_tool_enabled", "mute_tool_enabled",
    "welcome_enabled", "auto_accept_friend_request", "request_approval_enabled",
    "poke_stats_enabled", "qzone_auto_like_enabled", "qzone_dry_run",
})
LIST_KEYS = frozenset({
    "at_command_external_rules", "keyword_wakeup_words",
    "keyword_wakeup_group_whitelist", "keyword_wakeup_user_whitelist",
})
INT_RANGES = {
    "mute_duration_min": (1, 60),
    "mute_duration_max": (1, 43200),
    "poke_rank_max_limit": (10, 100),
    "qzone_poll_interval_sec": (120, 86400),
    "qzone_max_likes_per_cycle": (1, 10),
    "qzone_friend_batch_size": (0, 20),
    "qzone_feed_count": (10, 60),
    "qzone_delay_min_sec": (0, 120),
    "qzone_delay_max_sec": (0, 180),
}
QQ_KEYS = frozenset({"request_approval_qq", "qzone_owner_qq"})


def _error(web: Any, message: str, status: int = 400):
    return web.json_response({"status": "error", "message": message}, status_code=status)


def validate_settings(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("请提交至少一项设置")
    unknown = set(payload) - EDITABLE_KEYS
    if unknown:
        raise ValueError("包含不支持的配置项：" + ", ".join(sorted(unknown)))

    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if key in BOOL_KEYS:
            if type(value) is not bool:
                raise ValueError(f"{key} 必须为布尔值")
            clean[key] = value
        elif key in INT_RANGES:
            low, high = INT_RANGES[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} 必须为 {low}–{high} 之间的整数")
            clean[key] = value
        elif key in LIST_KEYS:
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError(f"{key} 必须是最多 100 项的列表")
            items: list[str] = []
            seen: set[str] = set()
            for item in value:
                if not isinstance(item, str) or len(item.strip()) > 200:
                    raise ValueError(f"{key} 中的每一项都必须是 200 字符以内的文本")
                item = item.strip()
                if not item:
                    continue
                if key in {"keyword_wakeup_group_whitelist", "keyword_wakeup_user_whitelist"} and not item.isdecimal():
                    raise ValueError(f"{key} 只允许填写数字 QQ 号或群号")
                folded = item.casefold()
                if folded not in seen:
                    items.append(item)
                    seen.add(folded)
            clean[key] = items
        elif key == "keyword_wakeup_mode":
            if value not in {"contains", "prefix"}:
                raise ValueError("关键词匹配方式只支持 contains 或 prefix")
            clean[key] = value
        elif key in QQ_KEYS:
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须是文本")
            value = value.strip()
            if value and not re.fullmatch(r"\d{5,20}", value):
                raise ValueError(f"{key} 必须是 5–20 位数字 QQ 号")
            clean[key] = value
        elif key == "welcome_message":
            if not isinstance(value, str) or len(value) > 2000:
                raise ValueError("欢迎消息必须是 2000 字符以内的文本")
            clean[key] = value
        else:
            raise ValueError(f"{key} 暂不支持在线修改")

    low = clean.get("mute_duration_min")
    high = clean.get("mute_duration_max")
    if low is not None and high is not None and low > high:
        raise ValueError("禁言最短时长不能大于最长时长")
    low = clean.get("qzone_delay_min_sec")
    high = clean.get("qzone_delay_max_sec")
    if low is not None and high is not None and low > high:
        raise ValueError("点赞随机延迟最小值不能大于最大值")
    return clean


def validate_combined_settings(current: dict[str, Any], updates: dict[str, Any]) -> None:
    merged = {**current, **updates}
    if "mute_duration_min" in updates or "mute_duration_max" in updates:
        if int(merged.get("mute_duration_min", 1)) > int(merged.get("mute_duration_max", 10)):
            raise ValueError("禁言最短时长不能大于最长时长")
    if "qzone_delay_min_sec" in updates or "qzone_delay_max_sec" in updates:
        if int(merged.get("qzone_delay_min_sec", 0)) > int(merged.get("qzone_delay_max_sec", 0)):
            raise ValueError("点赞随机延迟最小值不能大于最大值")
    if (updates.get("request_approval_enabled") is True
            or ("request_approval_qq" in updates and merged.get("request_approval_enabled"))) and not str(merged.get("request_approval_qq", "")).strip():
        raise ValueError("启用人工审批前，请先填写审批管理员 QQ")


def requires_confirmation(current: dict[str, Any], updates: dict[str, Any]) -> bool:
    """Require a second, explicit confirmation before enabling external actions."""
    for key in (
        "auto_accept_friend_request", "request_approval_enabled",
        "welcome_enabled", "mute_tool_enabled", "qzone_auto_like_enabled",
    ):
        if updates.get(key) is True and current.get(key) is not True:
            return True
    qzone_enabled = updates.get("qzone_auto_like_enabled", current.get("qzone_auto_like_enabled"))
    dry_run = updates.get("qzone_dry_run", current.get("qzone_dry_run"))
    if qzone_enabled and dry_run is False and current.get("qzone_dry_run") is not False:
        return True
    return False


def register_console_routes(context, plugin_name: str, plugin, *, web=None) -> None:
    if web is None:
        from astrbot.api import web

    async def state():
        try:
            snapshot = plugin.console_state()
            return web.json_response(snapshot)
        except Exception:
            return _error(web, "读取 Airi 控制中心状态失败，请检查插件日志。", 500)

    async def settings():
        payload = await web.request.json(default=None)
        if not isinstance(payload, dict) or set(payload) - {"settings", "confirm_side_effects"}:
            return _error(web, "请求格式不正确")
        try:
            updates = validate_settings(payload.get("settings"))
        except ValueError as exc:
            return _error(web, str(exc))
        confirmed = payload.get("confirm_side_effects", False)
        if type(confirmed) is not bool:
            return _error(web, "confirm_side_effects 必须为布尔值")
        current = plugin.console_state().get("settings", {})
        try:
            validate_combined_settings(current, updates)
        except ValueError as exc:
            return _error(web, str(exc))
        if requires_confirmation(current, updates) and not confirmed:
            return _error(web, "该设置会触发自动操作，请在页面确认后重试。", 409)
        try:
            await plugin.update_console_settings(updates)
        except (OSError, ValueError, RuntimeError) as exc:
            return _error(web, f"保存失败：{str(exc)[:180]}", 500)
        return await state()


    async def request_review():
        payload = await web.request.json(default=None)
        if not isinstance(payload, dict) or set(payload) != {"request_id", "approve", "confirm"}:
            return _error(web, "请求字段不正确")
        request_id = payload.get("request_id")
        approve = payload.get("approve")
        if payload.get("confirm") is not True:
            return _error(web, "请在页面确认审批操作后重试。", 409)
        if not isinstance(request_id, str) or not re.fullmatch(r"A\d{3,8}", request_id.strip().upper()):
            return _error(web, "申请编号格式不正确")
        if type(approve) is not bool:
            return _error(web, "审批动作必须是布尔值")
        bot = getattr(plugin, "_onebot_bot", None)
        if bot is None:
            return _error(web, "尚未捕获到 OneBot 客户端，请等待 Bot 收到一条 QQ 消息后重试。", 409)
        try:
            message = await plugin._request_approval.review_from_web(
                request_id, approve=approve, client=bot
            )
        except Exception as exc:
            return _error(web, f"审批失败：{str(exc)[:180]}", 500)
        if message.startswith("审批失败") or message.startswith("未找到"):
            return _error(web, message, 409)
        return web.json_response({"status": "ok", "message": message, "state": plugin.console_state()})

    async def qzone_scan():
        payload = await web.request.json(default=None)
        if not isinstance(payload, dict) or set(payload) - {"confirm_side_effects"}:
            return _error(web, "请求格式不正确")
        confirmed = payload.get("confirm_side_effects", False)
        if type(confirmed) is not bool:
            return _error(web, "confirm_side_effects 必须为布尔值")
        liker = plugin._qzone_liker
        if not liker.enabled:
            return _error(web, "请先在 QQ 空间设置中启用扫描功能。", 409)
        if liker._bot is None:
            return _error(web, "尚未捕获到 OneBot 账号，请等待 Bot 收到一条 QQ 消息后再扫描。", 409)
        if not liker.dry_run and not confirmed:
            return _error(web, "当前为真实点赞模式，请在页面确认后重试。", 409)
        try:
            summary = await liker.scan_once()
        except Exception as exc:
            return _error(web, f"扫描失败：{str(exc)[:180]}", 500)
        return web.json_response({"status": "ok", "summary": summary, "state": plugin.console_state()})

    for path, handler, method, description in (
        ("console/state", state, "GET", "读取 Airi 控制中心配置、统计与运行状态"),
        ("console/settings", settings, "POST", "保存并应用 Airi Core 可视化设置"),
        ("console/requests/review", request_review, "POST", "通过控制台审批好友申请或群邀请"),
        ("console/qzone/scan", qzone_scan, "POST", "手动扫描 QQ 空间好友新说说"),
    ):
        context.register_web_api(f"/{plugin_name}/{path}", handler, [method], description)
