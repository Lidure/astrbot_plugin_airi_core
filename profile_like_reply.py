"""Reply to incoming QQ profile-card likes via the OneBot v11 send_like action.

This module is independent of QQ Zone likes. It responds to OneBot
notify/profile_like events and also polls the read-only get_profile_like API
because some SnowLuma builds do not forward this push notification.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable


def parse_profile_like_notice(raw: Any, *, self_id: str = "") -> tuple[str, str, int, int] | None:
    """Return (bot_id, operator_id, event_time, notice_times) or None.

    SnowLuma/NapCat emit operator_id (not user_id) on profile_like notices.
    Reject own likes and malformed/unrelated events. The event's times is
    advisory and does not control the amount of the one-shot reply.
    """
    if not isinstance(raw, dict):
        return None
    if (raw.get("post_type"), raw.get("notice_type"), raw.get("sub_type")) != (
        "notice", "notify", "profile_like"
    ):
        return None
    bot = str(raw.get("self_id") or self_id or "").strip()
    operator = str(raw.get("operator_id") or "").strip()
    if not bot.isdecimal() or not operator.isdecimal() or int(operator) == 0:
        return None
    if self_id and bot != str(self_id):
        return None
    if operator == bot:
        return None
    try:
        occurred = int(raw.get("time") or 0)
        times = int(raw["times"]) if raw.get("times") is not None else 1
    except (ValueError, TypeError, OverflowError):
        return None
    if times <= 0:
        return None
    return bot, operator, occurred, min(times, 999)


class ProfileLikeReturner:
    """Once per bot/liker/local day, try to fill QQ's profile-card like quota.

    Other QQ plugins commonly attempt up to 50 likes in five send_like calls
    of 10 each. This is a single operation triggered by the first eligible
    notice that day, *not* a cooldown or a repeated background task.

    QQ may enforce a lower actual daily allowance or reject a request.
    Stop on the first rejection; do not claim that 50 were applied.
    """

    LIKE_BATCHES = (10, 10, 10, 10, 10)

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = False,
        logger: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.logger = logger
        self.clock = clock
        self._lock = asyncio.Lock()
        self._poll_lock = asyncio.Lock()
        self._state = self._load()
        self._bot: Any = None
        self._self_qq: str = ""
        self._poll_task: asyncio.Task | None = None
        self._poll_interval = 90

    def _load(self) -> dict:
        """Keep v1.3.20 per-user entries as already attempted for the day."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            accounts = data.get("accounts") if isinstance(data, dict) else None
            if isinstance(accounts, dict):
                polls = data.get("polls")
                return {
                    "version": 3,
                    "accounts": accounts,
                    "polls": polls if isinstance(polls, dict) else {},
                }
        except (OSError, ValueError, TypeError):
            pass
        return {"version": 3, "accounts": {}, "polls": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        try:
            with temp.open("w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
        finally:
            if temp.exists():
                temp.unlink()

    def _log(self, level: str, message: str) -> None:
        func = getattr(self.logger, level, None) if self.logger else None
        if callable(func):
            func("[ProfileLike] %s", message)

    @staticmethod
    def _api_succeeded(result: Any) -> bool:
        if result is False:
            return False
        if isinstance(result, dict):
            if result.get("status") == "failed":
                return False
            if result.get("retcode", 0) not in (0, None):
                return False
            if result.get("success") is False:
                return False
        return True

    @staticmethod
    def _is_quota_error(reason: Any) -> bool:
        text = str(reason or "").lower()
        return (
            "点赞数已达上限" in text
            or "今日点赞" in text and "上限" in text
            or "like limit" in text
        )

    async def handle_notice(
        self,
        raw: Any,
        send_like: Callable[[int, int], Awaitable[Any]],
        *,
        self_id: str = "",
        source: str = "notice",
    ) -> str:
        """Handle one notification; all batches belong to this single event."""
        if not self.enabled:
            return "disabled"
        parsed = parse_profile_like_notice(raw, self_id=self_id)
        if parsed is None:
            return "ignored"
        bot, operator, event_time, _ = parsed
        now = self.clock()
        # Ignore stale or future events after connection replay.
        max_age = 86400 if source == "poll" else 3600
        if event_time and (event_time > now + 300 or now - event_time > max_age):
            return "stale"
        day = datetime.fromtimestamp(now).date().isoformat()
        if event_time and datetime.fromtimestamp(event_time).date().isoformat() != day:
            # An event from before midnight must not consume tomorrow's quota.
            return "stale"
        async with self._lock:
            account = self._state["accounts"].setdefault(
                bot, {"date": day, "users": {}}
            )
            if account.get("date") != day:
                account["date"] = day
                account["users"] = {}
            # Old versions kept an extra event-fingerprint cache. Day-based
            # once-only replies supersede it; remove it on save.
            account.pop("events", None)
            users = account.setdefault("users", {})
            if not isinstance(users, dict):
                users = {}
                account["users"] = users
            if operator in users:
                return "already_done"

            # Write BEFORE the first API request. A timeout, reload, or
            # half-finished five-batch run must not trigger a second round.
            users[operator] = {
                "at": now, "submitted": 0, "status": "processing",
            }
            self._save()

            submitted = 0
            outcome = "sent"
            for times in self.LIKE_BATCHES:
                try:
                    response = await send_like(int(operator), times)
                except Exception as exc:
                    outcome = "limit" if self._is_quota_error(exc) else "failed"
                    self._log("warning",
                              f"资料卡回赞中断（{type(exc).__name__}）；"
                              f"本次已提交 {submitted} 个赞，今日不重复执行")
                    break
                if not self._api_succeeded(response):
                    reason = response.get("wording") or response.get("message") if isinstance(response, dict) else response
                    outcome = "limit" if self._is_quota_error(reason) else "failed"
                    self._log("warning",
                              f"资料卡回赞收到失败回执；本次已提交 {submitted} 个赞，"
                              "今日不重复执行")
                    break
                submitted += times
                users[operator]["submitted"] = submitted
                self._save()

            users[operator]["status"] = outcome
            self._save()
            if outcome == "sent":
                self._log("info", "收到资料卡点赞，已提交最多 50 个回赞（实际数量以 QQ 为准）")
            return outcome

    def attach_bot(self, bot: Any, self_qq: str) -> bool:
        """Capture the OneBot session from normal incoming events.

        No extra QQ account or password is configured. Never attach to a
        different bot ID after this instance has been bound.
        """
        if not self.enabled or bot is None:
            return False
        qq = str(self_qq or "").strip()
        if not qq.isdecimal() or int(qq) == 0:
            return False
        if self._self_qq and self._self_qq != qq:
            return False
        self._self_qq = qq
        self._bot = bot
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_worker())
            self._log("info", "资料卡回赞补充扫描已启动（每90秒）")
        return True

    async def close(self) -> None:
        task = self._poll_task
        self._poll_task = None
        self._bot = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _call_bot(self, action: str, **kwargs: Any) -> Any:
        bot = self._bot
        if bot is None:
            raise RuntimeError("OneBot 尚未连接")
        method = getattr(bot, action, None)
        if callable(method):
            reply = await method(**kwargs)
        else:
            callback = getattr(bot, "call_action", None)
            if not callable(callback):
                raise RuntimeError("OneBot 未提供 " + action)
            reply = await callback(action, **kwargs)
        if isinstance(reply, dict):
            if reply.get("status") == "failed" or reply.get("retcode", 0) not in (0, None):
                raise RuntimeError("OneBot " + action + " 返回失败")
            return reply.get("data", reply)
        return reply

    @staticmethod
    def _recent_voters(data: Any) -> list[tuple[str, int, int]]:
        """Only actual votes on the bot's *profile*, not favorites or posts."""
        if not isinstance(data, dict):
            raise ValueError("资料卡点赞查询结果不是对象")
        info = data.get("voteInfo")
        if not isinstance(info, dict) or not isinstance(info.get("userInfos"), list):
            raise ValueError("资料卡点赞查询缺少 voteInfo.userInfos")
        result: list[tuple[str, int, int]] = []
        for user in info["userInfos"]:
            if not isinstance(user, dict):
                continue
            qq = str(user.get("uin") or "").strip()
            if not qq.isdecimal() or int(qq) == 0:
                continue
            try:
                latest = int(user.get("latestTime") or 0)
                count = int(user.get("count") or 1)
            except (ValueError, TypeError, OverflowError):
                continue
            if latest > 0 and count > 0:
                result.append((qq, latest, min(999, count)))
        return result

    async def poll_once(self) -> dict[str, int]:
        """Read recent profile likers; invoke the same daily-deduped handler.

        First successful scan looks back 15 minutes to recover a just-missed
        notification. Later scans overlap by 5 minutes to handle API lag.
        Cursor is advanced only after a successful read, and is persisted.
        """
        stats = {"candidates": 0, "sent": 0, "already_done": 0, "failed": 0}
        if not self.enabled or not self._bot or not self._self_qq:
            return stats
        async with self._poll_lock:
            now = self.clock()
            old = self._state["polls"].get(self._self_qq)
            try:
                watermark = float(old) if old is not None else now - 900
            except (ValueError, TypeError, OverflowError):
                watermark = now - 900
            cutoff = max(
                watermark - (300 if old is not None else 0),
                datetime.fromtimestamp(now).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).timestamp(),
            )
            try:
                data = await self._call_bot(
                    "get_profile_like", user_id=int(self._self_qq),
                    start=0, count=100,
                )
                voters = self._recent_voters(data)
            except Exception as exc:
                self._log(
                    "warning",
                    f"资料卡点赞读取失败：{type(exc).__name__}，稍后重查",
                )
                return stats
            for operator, latest, times in sorted(voters, key=lambda x: x[1]):
                if not (cutoff <= latest <= now + 300):
                    continue
                if operator == self._self_qq:
                    continue
                stats["candidates"] += 1
                notice = {
                    "post_type": "notice", "notice_type": "notify",
                    "sub_type": "profile_like",
                    "self_id": int(self._self_qq),
                    "operator_id": int(operator),
                    "time": latest, "times": times,
                }
                try:
                    outcome = await self.handle_notice(
                        notice,
                        lambda uid, amount: self._call_bot(
                            "send_like", user_id=uid, times=amount
                        ),
                        self_id=self._self_qq, source="poll",
                    )
                except Exception as exc:
                    self._log("warning", f"资料卡补查处理异常：{type(exc).__name__}")
                    stats["failed"] += 1
                else:
                    if outcome in stats:
                        stats[outcome] += 1
            self._state["polls"][self._self_qq] = now
            self._save()
            if stats["candidates"]:
                self._log(
                    "info",
                    "资料卡补查：候选=%d 已回赞=%d 已处理跳过=%d 失败=%d"
                    % (stats["candidates"], stats["sent"],
                       stats["already_done"], stats["failed"]),
                )
            return stats

    async def _poll_worker(self) -> None:
        try:
            while self.enabled and self._bot is not None:
                await self.poll_once()
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            pass

