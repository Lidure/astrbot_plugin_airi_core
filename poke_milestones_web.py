"""Authenticated AstrBot Plugin View endpoints for poke milestones."""

import asyncio


def register_milestone_routes(context, plugin_name, store, *, web=None):
    if web is None:
        from astrbot.api import web

    def error(message, code):
        return web.json_response({"status": "error", "message": message}, status_code=code)

    async def state():
        if store is None:
            return error("里程碑数据读取失败，请检查插件日志。", 503)
        return web.json_response(await asyncio.to_thread(store.snapshot))

    async def settings():
        if store is None:
            return error("里程碑数据不可用。", 503)
        payload = await web.request.json(default=None)
        if not isinstance(payload, dict) or set(payload) - {"enabled", "confirm_side_effects"}:
            return error("请求字段不正确", 400)
        if type(payload.get("enabled")) is not bool:
            return error("开关必须是布尔值", 400)
        confirmed = payload.get("confirm_side_effects", False)
        if type(confirmed) is not bool:
            return error("确认标记必须是布尔值", 400)
        if payload["enabled"] and not confirmed:
            return error("开启里程碑会执行群禁言，请在页面确认后重试。", 409)
        try:
            await asyncio.to_thread(store.set_enabled, payload["enabled"])
        except OSError:
            return error("保存失败，请检查插件数据目录", 503)
        return await state()

    async def save_rule():
        if store is None:
            return error("里程碑数据不可用。", 503)
        payload = await web.request.json(default=None)
        if not isinstance(payload, dict):
            return error("请求体必须为 JSON 对象", 400)
        confirmed = payload.pop("confirm_side_effects", False)
        if type(confirmed) is not bool:
            return error("确认标记必须是布尔值", 400)
        if "command" in payload:
            return error("规则仅支持自动禁言", 400)
        if "id" in payload and (not isinstance(payload["id"], str) or not payload["id"].strip()):
            return error("规则 ID 无效", 400)
        enabled = payload.get("enabled", True)
        if type(enabled) is not bool:
            return error("开关必须是布尔值", 400)
        if store.snapshot().get("enabled") and enabled and not confirmed:
            return error("当前总开关已启用，保存启用规则可能触发群禁言，请确认后重试。", 409)
        try:
            await asyncio.to_thread(store.save_rule, payload)
        except ValueError as exc:
            return error(str(exc), 400)
        except KeyError:
            return error("规则不存在，请刷新", 404)
        except OSError:
            return error("写入失败，请检查插件数据目录", 503)
        return await state()

    async def toggle_rule():
        if store is None:
            return error("里程碑数据不可用。", 503)
        payload = await web.request.json(default=None)
        if not isinstance(payload, dict) or set(payload) - {"id", "enabled", "confirm_side_effects"}:
            return error("请求字段不正确", 400)
        if not {"id", "enabled"}.issubset(payload):
            return error("请求字段不完整", 400)
        if not isinstance(payload.get("id"), str) or not payload["id"].strip():
            return error("规则 ID 无效", 400)
        if type(payload.get("enabled")) is not bool:
            return error("开关必须是布尔值", 400)
        confirmed = payload.get("confirm_side_effects", False)
        if type(confirmed) is not bool:
            return error("确认标记必须是布尔值", 400)
        if store.snapshot().get("enabled") and payload["enabled"] and not confirmed:
            return error("当前总开关已启用，启用此规则可能触发群禁言，请确认后重试。", 409)
        try:
            await asyncio.to_thread(store.set_rule_enabled, payload["id"], payload["enabled"])
        except KeyError:
            return error("规则不存在，请刷新", 404)
        except ValueError as exc:
            return error(str(exc), 400)
        except OSError:
            return error("写入失败，请检查插件数据目录", 503)
        return await state()

    async def delete_rule():
        if store is None:
            return error("里程碑数据不可用。", 503)
        payload = await web.request.json(default=None)
        if not isinstance(payload, dict) or set(payload) != {"id"}:
            return error("请求字段不正确", 400)
        if not isinstance(payload.get("id"), str) or not payload["id"].strip():
            return error("规则 ID 无效", 400)
        try:
            await asyncio.to_thread(store.delete_rule, payload["id"])
        except KeyError:
            return error("规则不存在，请刷新", 404)
        except OSError:
            return error("写入失败，请检查插件数据目录", 503)
        return await state()

    for path, handler, verb, description in (
        ("state", state, "GET", "读取规则和最近禁言记录"),
        ("settings", settings, "POST", "设置里程碑总开关"),
        ("rules/save", save_rule, "POST", "新增或编辑禁言规则"),
        ("rules/toggle", toggle_rule, "POST", "启停单条规则"),
        ("rules/delete", delete_rule, "POST", "删除指定规则"),
    ):
        context.register_web_api(f"/{plugin_name}/poke-milestones/{path}", handler, [verb], description)
