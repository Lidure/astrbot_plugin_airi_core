"""Conservative @Bot command detection across AstrBot and external OneBot nodes.

We cannot discover commands registered inside other QQ nodes (gscore, Airi
node, etc.). Only an explicitly configured external rule can identify them.
This helper does NOT stop propagation or suppress plugins' outgoing replies.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


_REGISTERED_COMMAND_FILTERS = frozenset({"CommandFilter", "CommandGroupFilter"})
_SLASH_COMMAND = re.compile(r"^[/／][^/／\s?#][^\s]*")


def recognized_astrbot_command(event: Any) -> bool:
    """Check commands actually activated in AstrBot's waking stage.

    Do not treat generic listeners/regexes as commands. Their mere activation
    must never suppress natural @Bot chat. Avoid matching a guessed command
    registry, which may contain disabled or permission-restricted commands.
    """
    get_extra = getattr(event, "get_extra", None)
    if not callable(get_extra):
        return False
    handlers = get_extra("activated_handlers") or []
    for handler in handlers:
        for event_filter in getattr(handler, "event_filters", ()):
            if type(event_filter).__name__ in _REGISTERED_COMMAND_FILTERS:
                return True
    return False


def text_after_direct_at(parts: Iterable[Any], self_id: object, *,
                         at_type: type, plain_type: type) -> str:
    """Read only user-authored text directly after a leading @Bot component.

    Never inspect quoted, forwarded, or image content. An @Bot occurring after
    unrelated text is not interpreted as an external command.
    """
    found_at = False
    for part in parts:
        if not found_at:
            if isinstance(part, plain_type) and not str(part.text or "").strip():
                continue
            if isinstance(part, at_type) and str(part.qq) == str(self_id):
                found_at = True
                continue
            return ""
        if isinstance(part, plain_type):
            value = str(part.text or "").strip()
            if value:
                return value
            continue
        # Extra @ or other media before the command: don't guess.
        return ""
    return ""


def normalized_external_rules(value: object) -> tuple[str, ...]:
    """Accept a list of command names, =exact or ^prefix.

    Plain rules match a complete command or its whitespace-separated args.
    ^prefix rules intentionally accept concatenated parameters and should
    be used sparingly, as they can hide natural @Bot conversations.
    """
    if not isinstance(value, (list, tuple, set)):
        return ()
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        rule = item.strip()
        marker = rule[:1]
        target = rule[1:].strip() if marker in ("=", "^") else rule
        if not target or target.casefold() in {"/", "／"}:
            continue
        clean = marker + target if marker in ("=", "^") else target
        key = clean.casefold()
        if key not in seen:
            result.append(clean)
            seen.add(key)
    return tuple(result)


def matched_external_rule(text: str, rules: tuple[str, ...]) -> str | None:
    """Only anchored whole-command, token+args, or explicit ^prefix matching."""
    value = text.strip().casefold()
    for rule in rules:
        mark = rule[:1]
        if mark == "=":
            if value == rule[1:].casefold():
                return rule
        elif mark == "^":
            if value.startswith(rule[1:].casefold()):
                return rule
        else:
            word = rule.casefold()
            if value == word or (value.startswith(word) and
                                 len(value) > len(word) and
                                 value[len(word)].isspace()):
                return rule
    return None


def command_reason(event: Any, self_id: object, *, at_type: type,
                   plain_type: type, external_rules: tuple[str, ...],
                   auto_slash: bool = True) -> str | None:
    """Return why @Bot should NOT call the LLM, or None for ordinary chat."""
    if recognized_astrbot_command(event):
        return "astrbot"
    raw = text_after_direct_at(
        event.get_messages(), self_id, at_type=at_type, plain_type=plain_type
    )
    if not raw:
        return None
    if auto_slash and _SLASH_COMMAND.match(raw):
        return "slash"
    rule = matched_external_rule(raw, external_rules)
    if rule is not None:
        return "external"
    return None
