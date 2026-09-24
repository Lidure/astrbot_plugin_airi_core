from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple


class FriendRequestResult(NamedTuple):
    matched: bool
    approved: bool
    user_id: str | None = None
    error: str | None = None


def resolve_onebot_call_action(event: Any):
    """Return OneBot's call_action callable from an AstrBot message event.

    AstrBot's aiocqhttp adapter exposes CQHttp directly as ``event.bot``.
    Keep ``event.bot.api`` as a compatibility fallback for older wrappers/tests.
    """
    bot = getattr(event, "bot", None)
    direct = getattr(bot, "call_action", None)
    if callable(direct):
        return direct

    api = getattr(bot, "api", None)
    nested = getattr(api, "call_action", None)
    return nested if callable(nested) else None


async def maybe_accept_friend_request(
    raw_message: Any,
    call_action: Callable[..., Awaitable[Any]],
    *,
    enabled: bool,
) -> FriendRequestResult:
    if not enabled or not isinstance(raw_message, dict):
        return FriendRequestResult(False, False)
    if raw_message.get("post_type") != "request":
        return FriendRequestResult(False, False)
    if raw_message.get("request_type") != "friend":
        return FriendRequestResult(False, False)

    flag = raw_message.get("flag")
    if flag is None or str(flag).strip() == "":
        return FriendRequestResult(False, False)

    user_id_raw = raw_message.get("user_id")
    user_id = str(user_id_raw) if user_id_raw is not None else None

    try:
        await call_action(
            "set_friend_add_request",
            flag=str(flag),
            approve=True,
        )
    except Exception as exc:
        return FriendRequestResult(True, False, user_id, str(exc))

    return FriendRequestResult(True, True, user_id, None)
