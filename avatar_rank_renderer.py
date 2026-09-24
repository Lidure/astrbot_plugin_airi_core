from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

if __package__:
    from .avatar_cache import QqAvatarCache
    from .poke_stats import (
        _load_font,
        _truncate_label,
        privacy_safe_label,
        render_poke_rank_image as _render_base_rank_image,
    )
    from .rank_avatars import paste_circular_avatar
else:
    from avatar_cache import QqAvatarCache
    from poke_stats import (
        _load_font,
        _truncate_label,
        privacy_safe_label,
        render_poke_rank_image as _render_base_rank_image,
    )
    from rank_avatars import paste_circular_avatar


def _resolve_visible_avatars(
    entries: list[tuple[Any, Any]], cache: QqAvatarCache
) -> dict[str, str | None]:
    user_ids = [str(user_id) for user_id, _ in entries]
    if not user_ids:
        return {}
    workers = min(5, len(user_ids))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="airi-avatar"
    ) as executor:
        paths = list(executor.map(cache.get, user_ids))
    return dict(zip(user_ids, paths))


def render_poke_rank_image(
    output_path: str | os.PathLike[str],
    *,
    title: str,
    subtitle: str,
    summary: dict[str, Any],
    rank_limit: int = 10,
    avatar_cache: QqAvatarCache | None = None,
) -> Path:
    """Render the existing rank card and overlay circular QQ avatars."""
    output = _render_base_rank_image(
        output_path,
        title=title,
        subtitle=subtitle,
        summary=summary,
        rank_limit=rank_limit,
    )

    entries = list(summary.get("entries", []))[: max(1, int(rank_limit))]
    if not entries:
        return output

    cache = avatar_cache or QqAvatarCache(output.parent / "avatar_cache")
    avatar_paths = _resolve_visible_avatars(entries, cache)
    target_user_id = summary.get("target_user_id")
    if target_user_id is not None:
        target_user_id = str(target_user_id)
        if target_user_id not in avatar_paths:
            avatar_paths[target_user_id] = cache.get(target_user_id)
    display_names = summary.get("display_names") or {}

    with Image.open(output) as source:
        image = source.convert("RGB")

    draw = ImageDraw.Draw(image)
    body_font = _load_font(25, True)

    left = 34
    top_margin = 32
    header_h = 150
    gap = 20
    list_top = top_margin + header_h + gap
    row_h = 76

    for index, (user_id, _count) in enumerate(entries, start=1):
        user_id = str(user_id)
        y = list_top + 18 + (index - 1) * row_h
        row_fill = "#FFF4F8" if index <= 3 else "#FAF8FC"

        # Clear only the old username area; rank, bar and count stay untouched.
        draw.rectangle((left + 88, y + 6, left + 414, y + 54), fill=row_fill)

        avatar_box = (left + 94, y + 8, left + 138, y + 52)
        paste_circular_avatar(image, avatar_paths.get(user_id), avatar_box)

        display_name = privacy_safe_label(user_id, display_names.get(user_id))
        display_name = _truncate_label(display_name, 10)
        draw.text(
            (left + 150, y + 13),
            display_name,
            font=body_font,
            fill="#3B3242",
        )

    if target_user_id:
        list_h = max(160, 28 + len(entries) * row_h)
        query_y = list_top + list_h + gap
        right = 960 - 34
        draw.rectangle(
            (left + 120, query_y + 8, right - 20, query_y + 78),
            fill="#FFF0F6",
        )
        target_box = (left + 142, query_y + 19, left + 190, query_y + 67)
        paste_circular_avatar(image, avatar_paths.get(target_user_id), target_box)
        target_name = privacy_safe_label(
            target_user_id, display_names.get(target_user_id)
        )
        target_name = _truncate_label(target_name, 12)
        query_text = (
            f"{target_name}  累计 {int(summary.get('target_user_count') or 0)} 次"
        )
        draw.text(
            (left + 205, query_y + 27),
            query_text,
            font=body_font,
            fill="#D94E83",
        )

    temp_output = output.with_name(output.stem + ".avatar.tmp" + output.suffix)
    image.save(temp_output, format="PNG", optimize=True)
    os.replace(temp_output, output)
    return output
