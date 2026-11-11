"""Pure helpers for a safe, deterministic QQ group/private keyword-to-LLM wakeup."""
from __future__ import annotations

import re
from collections.abc import Iterable


def _split_list(value: object) -> list[str]:
    """Accept AstrBot's string-list widget and legacy comma/newline text."""
    if isinstance(value, str):
        values: Iterable[object] = [value]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        return []

    result: list[str] = []
    for item in values:
        if isinstance(item, str):
            result.extend(part.strip() for part in re.split(r"[,，;；\n\r]+", item))
        elif isinstance(item, int):
            result.append(str(item))
    return [entry for entry in result if entry]


def normalize_keywords(value: object) -> tuple[str, ...]:
    """Case-insensitive keywords; preserve the first appearance of each."""
    seen: set[str] = set()
    result: list[str] = []
    for entry in _split_list(value):
        folded = entry.casefold()
        if folded not in seen:
            seen.add(folded)
            result.append(folded)
    return tuple(result)


def normalize_allowlist(value: object) -> frozenset[str]:
    """Empty means unrestricted. A nonempty invalid entry never matches a QQ ID."""
    return frozenset(_split_list(value))


def match_keyword(text: str, keywords: tuple[str, ...], mode: str) -> str | None:
    """Return a match, or None; prefix ignores only leading whitespace.

    - contains: keyword anywhere in the user's actual message text
    - prefix: message text must start with the keyword
    Do not match QQ commands; command handling remains the responsibility of AstrBot.
    """
    if not isinstance(text, str) or not keywords:
        return None
    body = text.lstrip()
    if not body or body.startswith(("/", "／")):
        return None
    folded = body.casefold()
    for keyword in keywords:
        if (folded.startswith(keyword) if mode == "prefix" else keyword in folded):
            return keyword
    return None


def allowed_group_sender(
    group_id: object,
    sender_id: object,
    self_id: object,
    group_whitelist: frozenset[str],
    user_whitelist: frozenset[str],
) -> bool:
    """Allow normal human QQ group messages only, with two optional allowlists."""
    group = str(group_id or "").strip()
    sender = str(sender_id or "").strip()
    bot = str(self_id or "").strip()
    if not group or not sender or not bot or sender == bot:
        return False
    if group_whitelist and group not in group_whitelist:
        return False
    if user_whitelist and sender not in user_whitelist:
        return False
    return True

def allowed_private_sender(
    sender_id: object,
    self_id: object,
    user_whitelist: frozenset[str],
) -> bool:
    """Permit a QQ private sender, excluding the bot itself.

    Group allowlists do not apply to one-on-one conversations. The optional
    user allowlist has the same semantics for both group and private chats.
    """
    sender = str(sender_id or "").strip()
    bot = str(self_id or "").strip()
    if not sender or not bot or sender == bot:
        return False
    if user_whitelist and sender not in user_whitelist:
        return False
    return True
