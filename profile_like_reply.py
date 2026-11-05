"""Reply to incoming QQ profile-card likes via the OneBot v11 send_like action.

This module is independent of QQ Zone likes. It only reacts to a verified
notify/profile_like event and never polls or proactively likes strangers.
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
        self._state = self._load()

    def _load(self) -> dict:
        """Keep v1.3.20 per-user entries as already attempted for the day."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            accounts = data.get("accounts") if isinstance(data, dict) else None
            if isinstance(accounts, dict):
                return {"version": 2, "accounts": accounts}
        except (OSError, ValueError, TypeError):
            pass
        return {"version": 2, "accounts": {}}

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
        if event_time and (event_time > now + 300 or now - event_time > 3600):
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
