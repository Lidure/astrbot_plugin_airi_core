from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Awaitable, Callable

from PIL import Image, ImageDraw

if __package__:
    from .avatar_cache import QqAvatarCache, qq_group_avatar_url
    from .rank_avatars import paste_circular_avatar
    from .poke_stats import (
        _load_font,
        _rounded_gradient,
        _truncate_label,
        center_text_xy,
        format_rank_updated_label,
    )
else:
    from avatar_cache import QqAvatarCache, qq_group_avatar_url
    from rank_avatars import paste_circular_avatar
    from poke_stats import (
        _load_font,
        _rounded_gradient,
        _truncate_label,
        center_text_xy,
        format_rank_updated_label,
    )


def extract_onebot_group_names(payload: Any) -> dict[str, str]:
    """Accept get_group_list and get_group_info result envelopes."""
    if isinstance(payload, dict):
        payload = payload.get("data", payload)
        if isinstance(payload, dict):
            if "group_id" in payload:
                payload = [payload]
            else:
                payload = payload.get("groups", payload.get("group_list", []))
    if not isinstance(payload, list):
        return {}

    names: dict[str, str] = {}
    for group in payload:
        if not isinstance(group, dict):
            continue
        group_id = group.get("group_id")
        name = str(group.get("group_name") or "").strip().replace("\n", " ")
        if group_id is not None and name:
            names[str(group_id)] = name
    return names


async def resolve_group_display_names(
    call_action: Callable[..., Awaitable[Any]] | None,
    group_ids: list[str],
) -> dict[str, str]:
    """Resolve only visible group names; never rely on user QQ profile APIs."""
    if not callable(call_action) or not group_ids:
        return {}
    ids = list(dict.fromkeys(str(gid) for gid in group_ids))
    names: dict[str, str] = {}
    try:
        names = extract_onebot_group_names(await call_action("get_group_list"))
    except Exception:
        pass

    missing = [gid for gid in ids if gid not in names and gid.isdigit()]
    semaphore = asyncio.Semaphore(5)

    async def resolve_one(gid: str) -> tuple[str, str]:
        async with semaphore:
            try:
                payload = await call_action(
                    "get_group_info", group_id=int(gid), no_cache=False
                )
                return gid, extract_onebot_group_names(payload).get(gid, "")
            except Exception:
                return gid, ""

    if missing:
        for gid, name in await asyncio.gather(*(resolve_one(gid) for gid in missing)):
            if name:
                names[gid] = name
    return {gid: names[gid] for gid in ids if names.get(gid)}


def render_group_rank_image(
    output_path: str | os.PathLike[str],
    *,
    title: str,
    subtitle: str,
    summary: dict[str, Any],
    rank_limit: int = 10,
    avatar_cache: QqAvatarCache | None = None,
) -> Path:
    """Render a group leaderboard with cached circular QQ group portraits."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = list(summary.get("entries", []))[:max(1, int(rank_limit))]
    display_names = summary.get("display_names") or {}

    # Only fetch visible groups, in a bounded thread pool. Cache is isolated
    # from the user QQ avatar cache to avoid collisions between IDs.
    avatar_paths: dict[str, str | None] = {}
    if entries:
        cache = avatar_cache or QqAvatarCache(
            output.parent / "group_avatar_cache",
            url_builder=qq_group_avatar_url,
        )
        group_ids = [str(group_id) for group_id, _ in entries]

        def fetch_one(group_id: str) -> str | None:
            try:
                return cache.get(group_id)
            except Exception:
                return None

        with ThreadPoolExecutor(
            max_workers=min(5, len(group_ids)), thread_name_prefix="airi-group-avatar"
        ) as executor:
            avatar_paths = dict(zip(group_ids, executor.map(fetch_one, group_ids)))

    width = 960
    left, right = 34, 926
    top = 32
    header_h, gap, row_h, footer_h = 150, 20, 76, 128
    list_h = max(160, 28 + len(entries) * row_h)
    list_top = top + header_h + gap
    footer_y = list_top + list_h + gap
    height = footer_y + footer_h + 32

    image = Image.new("RGB", (width, height), "#FFF8FC")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        fraction = y / max(1, height - 1)
        light = (255, 247, 252)
        dark = (246, 243, 255)
        color = tuple(round(light[i] + (dark[i] - light[i]) * fraction) for i in range(3))
        draw.line((0, y, width, y), fill=color)

    draw.ellipse((790, -45, 1015, 180), fill="#FDE3EF")
    draw.ellipse((-70, height - 180, 130, height + 20), fill="#EDE7FF")
    _rounded_gradient(image, (left, top, right, top + header_h), "#FF84AF", "#B89CFF", 30)

    title_font = _load_font(38, True)
    subtitle_font = _load_font(20)
    small_font = _load_font(18)
    rank_font = _load_font(21, True)
    name_font = _load_font(25, True)
    count_font = _load_font(27, True)
    stat_label_font = _load_font(17)
    stat_value_font = _load_font(29, True)

    draw.text((left + 34, top + 28), title, font=title_font, fill="#FFFFFF")
    draw.text((left + 36, top + 84), subtitle, font=subtitle_font, fill="#FFF7FB")
    updated = format_rank_updated_label(summary.get("updated_at"))
    updated_width = draw.textbbox((0, 0), updated, font=small_font)[2]
    draw.text((right - updated_width - 32, top + 105), updated, font=small_font, fill="#FFF4FA")

    draw.rounded_rectangle(
        (left + 3, list_top + 6, right + 3, list_top + list_h + 6),
        radius=28, fill="#E8DCE8"
    )
    draw.rounded_rectangle(
        (left, list_top, right, list_top + list_h), radius=28, fill="#FFFFFF"
    )

    if not entries:
        draw.text(
            (left + 48, list_top + 50), "暂无群聊 Poke 记录",
            font=name_font, fill="#493E54"
        )
        draw.text(
            (left + 48, list_top + 94), "有人在群聊中戳 Bot 后就会出现排名～",
            font=subtitle_font, fill="#8C8194"
        )
    else:
        medals = ("#F8C95F", "#C8CFDE", "#DCA47B")
        max_count = max(int(count) for _, count in entries)
        for position, (raw_group_id, raw_count) in enumerate(entries, start=1):
            group_id = str(raw_group_id)
            count = int(raw_count)
            y = list_top + 18 + (position - 1) * row_h
            draw.rounded_rectangle(
                (left + 20, y, right - 20, y + 60), radius=19,
                fill="#FFF4F8" if position <= 3 else "#FAF8FC"
            )
            circle_x = left + 42
            circle_y = y + 10
            draw.ellipse(
                (circle_x, circle_y, circle_x + 40, circle_y + 40),
                fill=medals[position - 1] if position <= 3 else "#E9E4EE"
            )
            x, rank_y = center_text_xy(
                draw, str(position), rank_font, (circle_x + 20, circle_y + 20)
            )
            draw.text((x, rank_y), str(position), font=rank_font, fill="#4B4051")

            paste_circular_avatar(
                image,
                avatar_paths.get(group_id),
                (left + 110, y + 8, left + 154, y + 52),
            )

            name = str(display_names.get(group_id) or "").strip()
            if not name:
                name = "未知群聊"
            draw.text(
                (left + 170, y + 4), _truncate_label(name, 14),
                font=name_font, fill="#3B3242"
            )
            draw.text(
                (left + 172, y + 36), f"群号 {group_id}",
                font=small_font, fill="#918697"
            )

            bar_left, bar_right = left + 565, right - 148
            draw.rounded_rectangle(
                (bar_left, y + 25, bar_right, y + 37),
                radius=6, fill="#EEE8F1"
            )
            ratio = max(0.06, count / max(1, max_count))
            draw.rounded_rectangle(
                (bar_left, y + 25, bar_left + int((bar_right - bar_left) * ratio), y + 37),
                radius=6, fill="#F29BB9" if position <= 3 else "#C2AFE9"
            )
            text = f"{count} 次"
            text_width = draw.textbbox((0, 0), text, font=count_font)[2]
            draw.text(
                (right - 44 - text_width, y + 11),
                text, font=count_font, fill="#F05F95"
            )

    draw.rounded_rectangle(
        (left, footer_y, right, footer_y + footer_h), radius=26, fill="#FFFFFF"
    )
    label = "今日被戳" if summary.get("is_daily") else "历史被戳"
    stats = [
        (label, f"{int(summary.get('total_pokes', 0))} 次"),
        ("参与群聊", f"{int(summary.get('total_groups', 0))} 个"),
        ("榜单显示", f"{len(entries)} 个"),
    ]
    col_w = (right - left - 44) // 3
    for i, (stat_label, stat_value) in enumerate(stats):
        x = left + 22 + i * col_w
        if i:
            draw.line(
                (x - 12, footer_y + 26, x - 12, footer_y + footer_h - 26),
                fill="#EFE8F1", width=2
            )
        draw.text(
            (x + 12, footer_y + 27), stat_label,
            font=stat_label_font, fill="#918697"
        )
        draw.text(
            (x + 12, footer_y + 60), stat_value,
            font=stat_value_font, fill="#4A3E52"
        )

    temp_output = output.with_name(output.stem + ".tmp" + output.suffix)
    image.save(temp_output, format="PNG", optimize=True)
    os.replace(temp_output, output)
    return output
