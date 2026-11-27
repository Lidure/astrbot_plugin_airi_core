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

    def writer(method, keys=(), *, rule=False):
        async def handle():
            if store is None:
                return error("里程碑数据不可用。", 503)
            payload = await web.request.json(default=None)
            if not isinstance(payload, dict):
                return error("请求体必须为 JSON 对象", 400)
            if not rule and set(keys) != set(payload):
                return error("请求字段不正确", 400)
            if "id" in payload and (not isinstance(payload["id"], str) or not payload["id"].strip()):
                return error("规则 ID 无效", 400)
            if "enabled" in payload and type(payload["enabled"]) is not bool:
                return error("开关必须是布尔值", 400)
            if rule and "command" in payload:
                return error("规则仅支持自动禁言", 400)
            args = (payload,) if rule else tuple(payload[k] for k in keys)
            try:
                await asyncio.to_thread(getattr(store, method), *args)
            except ValueError as exc:
                return error(str(exc), 400)
            except KeyError:
                return error("规则不存在，请刷新", 404)
            except OSError:
                return error("写入失败，请检查插件数据目录", 503)
            return await state()
        return handle

    for path, handler, verb, description in (
        ("state", state, "GET", "读取规则和最近禁言记录"),
        ("settings", writer("set_enabled", ("enabled",)), "POST", "设置里程碑总开关"),
        ("rules/save", writer("save_rule", rule=True), "POST", "新增或编辑禁言规则"),
        ("rules/toggle", writer("set_rule_enabled", ("id", "enabled")), "POST", "启停单条规则"),
        ("rules/delete", writer("delete_rule", ("id",)), "POST", "删除指定规则"),
    ):
        context.register_web_api(f"/{plugin_name}/poke-milestones/{path}", handler, [verb], description)
