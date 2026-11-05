"""Reply to incoming QQ profile-card likes via the OneBot v11 send_like action.

This module is independent of QQ Zone likes. It only reacts to a verified
notify/profile_like event and never polls or proactively likes strangers.
"""
from __future__ import annotations

import asyncio
import hashlib
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
    advisory; the configured reply amount is a separate, bounded setting.
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
        times = int(raw.get("times") or 1)
    except (ValueError, TypeError, OverflowError):
        return None
    if times <= 0:
        return None
    return bot, operator, occurred, min(times, 999)


class ProfileLikeReturner:
    """Rate-limited, restart-safe automatic profile-card reciprocity.

    One event results in one configurable send_like call. Reserve the attempt
    on disk *before* the API call to avoid duplicate likes on hot reload.
    There is deliberately no unbounded retry after an ambiguous API result.
    """

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = False,
        reply_times: int = 1,
        daily_limit: int = 10,
        cooldown_sec: int = 120,
        logger: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.reply_times = max(1, min(10, int(reply_times)))
        self.daily_limit = max(1, min(10, int(daily_limit)))
        self.cooldown_sec = max(30, min(86400, int(cooldown_sec)))
        self.logger = logger
        self.clock = clock
        self._lock = asyncio.Lock()
        self._state = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            accounts = data.get("accounts") if isinstance(data, dict) else None
            if isinstance(accounts, dict):
                return {"version": 1, "accounts": accounts}
        except (OSError, ValueError, TypeError):
            pass
        return {"version": 1, "accounts": {}}

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
        if isinstance(result, dict):
            if result.get("status") == "failed":
                return False
            if result.get("retcode", 0) not in (0, None):
                return False
            if result.get("success") is False:
                return False
        return True

    async def handle_notice(
        self,
        raw: Any,
        send_like: Callable[[int, int], Awaitable[Any]],
        *,
        self_id: str = "",
    ) -> str:
        """Process a genuine notification, return a diagnostic status string."""
        if not self.enabled:
            return "disabled"
        parsed = parse_profile_like_notice(raw, self_id=self_id)
        if parsed is None:
            return "ignored"
        bot, operator, event_time, count = parsed
        now = self.clock()
        # Do not reply to a stale replayed notice after reconnection.
        if event_time and (event_time > now + 300 or now - event_time > 3600):
            return "stale"
        date = datetime.fromtimestamp(now).date().isoformat()
        fingerprint = hashlib.sha256(
            f"{bot}|{operator}|{event_time}|{count}".encode("ascii")
        ).hexdigest()
        async with self._lock:
            account = self._state["accounts"].setdefault(
                bot, {"date": date, "users": {}, "events": {}}
            )
            if account.get("date") != date:
                account["date"] = date
                account["users"] = {}
            events = account.setdefault("events", {})
            if not isinstance(events, dict):
                events = {}
                account["events"] = events
            # Keep recent event fingerprints across local midnight/restarts.
            for key, stamp in list(events.items()):
                try:
                    outdated = now - float(stamp) > 172800
                except (ValueError, TypeError, OverflowError):
                    outdated = True
                if outdated:
                    events.pop(key, None)
            if len(events) > 5000:
                for key, _ in sorted(events.items(), key=lambda x: x[1])[:1000]:
                    events.pop(key, None)
            if fingerprint in events:
                return "duplicate"

            users = account.setdefault("users", {})
            if not isinstance(users, dict):
                users = {}
                account["users"] = users
            user = users.setdefault(operator, {"attempted": 0, "last_at": 0.0})
            try:
                attempts = max(0, int(user.get("attempted", 0)))
                last_at = float(user.get("last_at", 0))
            except (ValueError, TypeError, OverflowError):
                attempts, last_at = 0, 0.0
            if last_at > 0 and now - last_at < self.cooldown_sec:
                return "cooldown"
            remaining = self.daily_limit - attempts
            if remaining <= 0:
                return "daily_limit"
            times = min(self.reply_times, remaining)
            # The durable reservation is intentional: never resend the same
            # event if AstrBot hot-reloads during an in-flight send_like.
            events[fingerprint] = now
            user["attempted"] = attempts + times
            user["last_at"] = now
            self._save()
            try:
                response = await send_like(int(operator), times)
            except Exception as exc:
                self._log("warning", f"资料卡回赞请求失败：{type(exc).__name__}；本事件不重复提交")
                return "failed"
            if not self._api_succeeded(response):
                self._log("warning", "资料卡回赞接口返回失败；本事件不重复提交")
                return "failed"
            self._log("info", f"收到资料卡点赞，已回赞 {times} 次（今日该用户已尝试 {attempts + times} 次）")
            return "sent"
