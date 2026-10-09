from __future__ import annotations

from typing import Any

DEFAULT_RANK_LIMIT = 10
DEFAULT_RANK_MAX_LIMIT = 100
MIN_RANK_MAX_LIMIT = 10
HARD_MAX_RANK_LIMIT = 100


def normalize_rank_max_limit(value: Any) -> int:
    """Normalize the configurable maximum number of visible rank rows."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_RANK_MAX_LIMIT
    return max(MIN_RANK_MAX_LIMIT, min(HARD_MAX_RANK_LIMIT, parsed))


def parse_rank_command_args(
    arg1: Any,
    arg2: Any,
    *,
    max_limit: int,
    command_name: str,
) -> tuple[int | None, str | None, str | None]:
    """Parse optional rank size and QQ while keeping the legacy QQ-only syntax.

    Supported forms:
      /command
      /command 50
      /command 123456789
      /command 50 123456789

    A single 5+ digit number is treated as the legacy QQ-only query. Rank sizes
    are intentionally capped by max_limit instead of raising an error.
    """
    max_limit = normalize_rank_max_limit(max_limit)
    first = str(arg1 or "").strip()
    second = str(arg2 or "").strip()

    if second:
        if not first.isdigit() or int(first) <= 0:
            return (
                None,
                None,
                f"排行人数必须是正整数，例如：/{command_name} 50 123456789",
            )
        if not second.isdigit():
            return (
                None,
                None,
                f"QQ 号必须是纯数字，例如：/{command_name} 50 123456789",
            )
        return min(int(first), max_limit), second, None

    if not first:
        return DEFAULT_RANK_LIMIT, None, None

    if not first.isdigit():
        return (
            None,
            None,
            f"参数必须是排行人数或 QQ 号，例如：/{command_name} 50",
        )

    # Preserve the historical one-argument QQ query syntax. QQ numbers are
    # normally at least five digits, while this plugin caps rank rows at 100.
    if len(first) >= 5:
        return DEFAULT_RANK_LIMIT, first, None

    requested = int(first)
    if requested <= 0:
        return None, None, f"排行人数必须是正整数，例如：/{command_name} 50"

    return min(requested, max_limit), None, None
