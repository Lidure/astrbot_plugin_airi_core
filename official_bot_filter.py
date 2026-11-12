"""Identify QQ *official* bots using OneBot/SnowLuma group-member metadata.

SnowLuma derives get_group_member_info.is_robot from QQ robot-UIN ranges.
No QQ-number heuristics, nickname checks, or 'looks like a bot' guesses.
On unsupported OneBot adapters or lookup failure, preserve normal messages.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any


def robot_flag(info: Any) -> bool | None:
    """Only an explicit boolean is_robot is authoritative."""
    if not isinstance(info, dict):
        return None
    if "data" in info and isinstance(info["data"], dict):
        info = info["data"]
    flag = info.get("is_robot")
    return flag if type(flag) is bool else None


class OfficialQQBotClassifier:
    """Bounded TTL cache avoids re-querying group members on every @."""

    def __init__(self, *, ttl_seconds: int = 3600, max_entries: int = 512,
                 timeout_seconds: float = 3.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self._cache: dict[tuple[str, str], tuple[bool, float]] = {}

    async def is_official(self, event: Any, call_action: Any) -> bool:
        """Only checks actual QQ group messages; never block uncertain identities."""
        try:
            if event.is_private_chat():
                return False
            group_id = str(event.get_group_id() or "").strip()
            sender_id = str(event.get_sender_id() or "").strip()
            bot_id = str(event.get_self_id() or "").strip()
        except (TypeError, ValueError, AttributeError):
            return False
        if not group_id.isdecimal() or not sender_id.isdecimal():
            return False
        if not bot_id or sender_id == bot_id:
            return False

        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            flag = robot_flag(raw.get("sender"))
            if flag is True:
                return True
            if flag is False:
                return False

        key = (group_id, sender_id)
        now = self.clock()
        cached = self._cache.get(key)
        if cached and cached[1] > now:
            return cached[0]
        if cached:
            self._cache.pop(key, None)

        if not callable(call_action):
            return False

        try:
            response = await asyncio.wait_for(
                call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(sender_id),
                    no_cache=False,
                ),
                timeout=self.timeout_seconds,
            )
        except Exception:
            # Unsupported adapter / QQ read failure / temporary timeout:
            # unknown is NOT an official bot, to avoid false positives.
            return False

        result = robot_flag(response)
        if result is None:
            return False

        self._cache[key] = (result, self.clock() + self.ttl_seconds)
        if len(self._cache) > self.max_entries:
            # Insertion-ordered dict; remove oldest recorded classification.
            self._cache.pop(next(iter(self._cache)))
        return result
