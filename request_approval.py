from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any


class PendingRequestStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"next_id": 1, "requests": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {"next_id": 1, "requests": {}}
        if not isinstance(data, dict):
            return {"next_id": 1, "requests": {}}
        next_id = data.get("next_id", 1)
        requests = data.get("requests", {})
        try:
            next_id = max(1, int(next_id))
        except (TypeError, ValueError):
            next_id = 1
        if not isinstance(requests, dict):
            requests = {}
        return {"next_id": next_id, "requests": requests}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass

    def add(self, request: Mapping[str, Any]) -> dict[str, Any]:
        request_id = f"A{int(self._data['next_id']):03d}"
        self._data["next_id"] = int(self._data["next_id"]) + 1
        item = dict(request)
        item["request_id"] = request_id
        item.setdefault("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._data["requests"][request_id] = item
        self._save()
        return dict(item)

    def get(self, request_id: str) -> dict[str, Any] | None:
        item = self._data.get("requests", {}).get(str(request_id).strip().upper())
        return dict(item) if isinstance(item, dict) else None

    def remove(self, request_id: str) -> dict[str, Any] | None:
        key = str(request_id).strip().upper()
        item = self._data.get("requests", {}).pop(key, None)
        if item is not None:
            self._save()
        return dict(item) if isinstance(item, dict) else None


class RequestApprovalManager:
    def __init__(self, path: str | Path):
        self.store = PendingRequestStore(path)

    @staticmethod
    def _parse_raw(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, Mapping) or raw.get("post_type") != "request":
            return None

        flag = str(raw.get("flag") or "").strip()
        if not flag:
            return None

        request_type = raw.get("request_type")
        if request_type == "friend":
            return {
                "kind": "friend",
                "flag": flag,
                "user_id": str(raw.get("user_id") or ""),
                "comment": str(raw.get("comment") or "无"),
            }

        if request_type == "group" and raw.get("sub_type") == "invite":
            return {
                "kind": "group_invite",
                "flag": flag,
                "user_id": str(raw.get("user_id") or ""),
                "group_id": str(raw.get("group_id") or ""),
                "comment": str(raw.get("comment") or "无"),
            }
        return None

    @staticmethod
    async def _safe_nickname(client: Any, user_id: str) -> str:
        try:
            info = await client.get_stranger_info(user_id=int(user_id))
            if isinstance(info, dict):
                return str(info.get("nickname") or info.get("nick") or "未知昵称")
        except Exception:
            pass
        return "未知昵称"

    @staticmethod
    async def _safe_group_name(client: Any, group_id: str) -> str:
        try:
            info = await client.get_group_info(group_id=int(group_id))
            if isinstance(info, dict):
                return str(info.get("group_name") or "未知群名")
        except Exception:
            pass
        return "未知群名"

    @staticmethod
    def _friend_message(item: Mapping[str, Any]) -> str:
        rid = item["request_id"]
        return (
            f"【好友申请】{rid}\n"
            f"昵称：{item.get('nickname') or '未知昵称'}\n"
            f"QQ：{item.get('user_id') or '未知'}\n"
            f"验证信息：{item.get('comment') or '无'}\n\n"
            f"回复 /同意申请 {rid} 或 /拒绝申请 {rid}"
        )

    @staticmethod
    def _group_message(item: Mapping[str, Any]) -> str:
        rid = item["request_id"]
        return (
            f"【群邀请】{rid}\n"
            f"邀请人：{item.get('nickname') or '未知昵称'} ({item.get('user_id') or '未知'})\n"
            f"群聊：{item.get('group_name') or '未知群名'} ({item.get('group_id') or '未知'})\n"
            f"验证信息：{item.get('comment') or '无'}\n\n"
            f"回复 /同意申请 {rid} 或 /拒绝申请 {rid}"
        )

    async def handle_request(
        self,
        event: Any,
        *,
        auto_accept_friend: bool,
        approval_enabled: bool,
        approval_qq: str,
    ) -> str:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        request = self._parse_raw(raw)
        if not request:
            return "ignored"

        client = getattr(event, "bot", None)
        if client is None:
            return "missing_client"

        if request["kind"] == "friend" and auto_accept_friend:
            try:
                await client.set_friend_add_request(
                    flag=request["flag"], approve=True
                )
            except Exception:
                return "auto_approve_failed"
            return "auto_approved_friend"

        approval_qq = str(approval_qq or "").strip()
        if not approval_enabled or not approval_qq.isdigit():
            return "ignored"

        request["nickname"] = await self._safe_nickname(client, request["user_id"])
        if request["kind"] == "group_invite":
            request["group_name"] = await self._safe_group_name(
                client, request["group_id"]
            )

        item = self.store.add(request)
        message = (
            self._friend_message(item)
            if item["kind"] == "friend"
            else self._group_message(item)
        )
        try:
            await client.send_private_msg(user_id=int(approval_qq), message=message)
        except Exception:
            self.store.remove(item["request_id"])
            return "notify_failed"
        return "queued_friend" if item["kind"] == "friend" else "queued_group_invite"

    async def review(
        self,
        event: Any,
        request_id: str,
        *,
        approve: bool,
        approval_qq: str,
    ) -> str:
        sender_id = str(event.get_sender_id() if hasattr(event, "get_sender_id") else "")
        approval_qq = str(approval_qq or "").strip()
        if not approval_qq or sender_id != approval_qq:
            return "你没有审批权限。"

        request_id = str(request_id or "").strip().upper()
        if not request_id:
            return "请提供申请编号，例如：/同意申请 A001"

        item = self.store.get(request_id)
        if not item:
            return f"未找到待审批申请 {request_id}，可能已处理或编号有误。"

        client = getattr(event, "bot", None)
        if client is None:
            return "审批失败：当前 OneBot 客户端不可用。"

        try:
            if item.get("kind") == "friend":
                await client.set_friend_add_request(
                    flag=str(item.get("flag") or ""), approve=approve
                )
                label = "好友申请"
            elif item.get("kind") == "group_invite":
                await client.set_group_add_request(
                    flag=str(item.get("flag") or ""),
                    sub_type="invite",
                    approve=approve,
                )
                label = "群邀请"
            else:
                return f"审批失败：申请 {request_id} 类型无效。"
        except Exception as exc:
            return f"审批失败：{exc}。申请 {request_id} 已保留，可稍后重试。"

        self.store.remove(request_id)
        action = "同意" if approve else "拒绝"
        return f"已{action}{label} {request_id}。"
