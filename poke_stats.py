from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


STATS_CUTOFF_HOUR = 4


def stats_day_key(now: datetime | None = None, cutoff_hour: int = STATS_CUTOFF_HOUR) -> str:
    """Return the logical stats date, where each day starts at ``cutoff_hour`` local time."""
    moment = now or datetime.now()
    shifted = moment - timedelta(hours=int(cutoff_hour))
    return shifted.date().isoformat()


def parse_bot_poke_notice(raw_message: Any) -> tuple[str, str] | None:
    if not isinstance(raw_message, dict):
        return None
    if raw_message.get("post_type") != "notice":
        return None
    if raw_message.get("notice_type") != "notify" or raw_message.get("sub_type") != "poke":
        return None
    group_id = raw_message.get("group_id")
    user_id = raw_message.get("user_id")
    target_id = raw_message.get("target_id")
    self_id = raw_message.get("self_id")
    if group_id is None or user_id is None or target_id is None or self_id is None:
        return None
    if str(target_id) != str(self_id) or str(user_id) == str(self_id):
        return None
    return str(group_id), str(user_id)


def extract_onebot_profile_name(payload: Any, *, prefer_card: bool) -> str:
    if not isinstance(payload, dict):
        data = getattr(payload, "data", None)
        if not isinstance(data, dict):
            return ""
    else:
        nested = payload.get("data")
        data = nested if isinstance(nested, dict) else payload
    nickname = str(data.get("nickname") or "").strip()
    card = str(data.get("card") or "").strip()
    if prefer_card and card:
        return card
    return nickname


def privacy_safe_label(user_id: str, preferred_name: str | None = None) -> str:
    name = str(preferred_name or "").strip()
    if name:
        return name
    digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:4].upper()
    return f"匿名用户 {digest}"


def _truncate_label(label: str, max_chars: int = 14) -> str:
    label = str(label or "").strip()
    if len(label) <= max_chars:
        return label
    return label[: max_chars - 1] + "…"


class PokeStatsStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {
            "version": 2,
            "groups": {},
            "daily": {"key": None, "groups": {}},
            "meta": {"updated_at": None},
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty_data()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return self._empty_data()
        if not isinstance(loaded, dict):
            return self._empty_data()

        groups = loaded.get("groups")
        if not isinstance(groups, dict):
            groups = {}
        meta = loaded.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        daily = loaded.get("daily")
        if not isinstance(daily, dict):
            daily = {}
        daily_groups = daily.get("groups")
        if not isinstance(daily_groups, dict):
            daily_groups = {}
        daily_key = daily.get("key")
        if daily_key is not None:
            daily_key = str(daily_key)

        return {
            "version": 2,
            "groups": groups,
            "daily": {"key": daily_key, "groups": daily_groups},
            "meta": {"updated_at": meta.get("updated_at")},
        }

    def _save(self) -> None:
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)

    @staticmethod
    def _increment(groups: dict[str, Any], group_id: str, user_id: str) -> int:
        group = groups.setdefault(group_id, {"users": {}, "total": 0})
        users = group.setdefault("users", {})
        user = users.setdefault(user_id, {"count": 0})
        user["count"] = int(user.get("count", 0)) + 1
        group["total"] = int(group.get("total", 0)) + 1
        return user["count"]

    def _daily_groups_for_write(self, day_key: str) -> dict[str, Any]:
        daily = self.data.setdefault("daily", {"key": None, "groups": {}})
        if daily.get("key") != day_key or not isinstance(daily.get("groups"), dict):
            daily["key"] = day_key
            daily["groups"] = {}
        return daily["groups"]

    def _daily_groups_for_read(self, day_key: str) -> dict[str, Any]:
        daily = self.data.get("daily", {})
        if not isinstance(daily, dict) or daily.get("key") != day_key:
            return {}
        groups = daily.get("groups")
        return groups if isinstance(groups, dict) else {}

    def record(self, group_id: str, user_id: str, *, now: datetime | None = None) -> int:
        group_id = str(group_id)
        user_id = str(user_id)
        moment = now or datetime.now()
        historical_groups = self.data.setdefault("groups", {})
        historical_count = self._increment(historical_groups, group_id, user_id)
        daily_groups = self._daily_groups_for_write(stats_day_key(moment))
        self._increment(daily_groups, group_id, user_id)
        self.data.setdefault("meta", {})["updated_at"] = moment.strftime("%Y-%m-%d %H:%M:%S")
        self.data["version"] = 2
        self._save()
        return historical_count

    def _group_summary(
        self,
        groups: dict[str, Any],
        group_id: str,
        target_user_id: str | None,
        *,
        updated_at: str | None,
        stats_day: str | None = None,
    ) -> dict[str, Any]:
        group = groups.get(str(group_id), {}) if isinstance(groups, dict) else {}
        users = group.get("users", {}) if isinstance(group, dict) else {}
        if not isinstance(users, dict):
            users = {}
        entries = sorted(
            (
                (str(uid), int(info.get("count", 0)))
                for uid, info in users.items()
                if isinstance(info, dict)
            ),
            key=lambda item: (-item[1], item[0]),
        )
        target_count = None
        if target_user_id is not None:
            info = users.get(str(target_user_id), {})
            target_count = int(info.get("count", 0)) if isinstance(info, dict) else 0
        total_pokes = int(group.get("total", 0)) if isinstance(group, dict) else 0
        ranked_groups = sorted(
            (
                (str(gid), int(info.get("total", 0)))
                for gid, info in groups.items()
                if isinstance(info, dict) and int(info.get("total", 0)) > 0
            ),
            key=lambda item: (-item[1], item[0]),
        )
        group_total_rank = next(
            (index for index, (gid, _) in enumerate(ranked_groups, start=1) if gid == str(group_id)),
            None,
        )
        return {
            "entries": entries,
            "total_pokes": total_pokes,
            "unique_users": len(users),
            "target_user_id": str(target_user_id) if target_user_id is not None else None,
            "target_user_count": target_count,
            "updated_at": updated_at,
            "stats_day": stats_day,
            "group_total_rank": group_total_rank,
            "group_total_groups": len(ranked_groups),
        }

    def _global_summary(
        self,
        groups: dict[str, Any],
        target_user_id: str | None,
        *,
        updated_at: str | None,
        stats_day: str | None = None,
    ) -> dict[str, Any]:
        merged: dict[str, int] = {}
        total_pokes = 0
        if not isinstance(groups, dict):
            groups = {}
        for group in groups.values():
            if not isinstance(group, dict):
                continue
            total_pokes += int(group.get("total", 0))
            users = group.get("users", {})
            if not isinstance(users, dict):
                continue
            for uid, info in users.items():
                if isinstance(info, dict):
                    merged[str(uid)] = merged.get(str(uid), 0) + int(info.get("count", 0))
        entries = sorted(merged.items(), key=lambda item: (-item[1], item[0]))
        target_count = merged.get(str(target_user_id), 0) if target_user_id is not None else None
        return {
            "entries": entries,
            "total_pokes": total_pokes,
            "unique_users": len(merged),
            "target_user_id": str(target_user_id) if target_user_id is not None else None,
            "target_user_count": target_count,
            "updated_at": updated_at,
            "stats_day": stats_day,
        }

    def group_summary(self, group_id: str, target_user_id: str | None = None) -> dict[str, Any]:
        return self._group_summary(
            self.data.get("groups", {}),
            group_id,
            target_user_id,
            updated_at=self.data.get("meta", {}).get("updated_at"),
        )

    def global_summary(self, target_user_id: str | None = None) -> dict[str, Any]:
        return self._global_summary(
            self.data.get("groups", {}),
            target_user_id,
            updated_at=self.data.get("meta", {}).get("updated_at"),
        )

    def daily_group_summary(
        self,
        group_id: str,
        target_user_id: str | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        key = stats_day_key(now)
        groups = self._daily_groups_for_read(key)
        updated_at = self.data.get("meta", {}).get("updated_at") if groups else None
        return self._group_summary(
            groups,
            group_id,
            target_user_id,
            updated_at=updated_at,
            stats_day=key,
        )

    def daily_global_summary(
        self,
        target_user_id: str | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        key = stats_day_key(now)
        groups = self._daily_groups_for_read(key)
        updated_at = self.data.get("meta", {}).get("updated_at") if groups else None
        return self._global_summary(
            groups,
            target_user_id,
            updated_at=updated_at,
            stats_day=key,
        )


@lru_cache(maxsize=32)
def _load_font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKSC-Bold.otf" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJKSC-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _rounded_gradient(image: Image.Image, box, start_color, end_color, radius: int) -> None:
    x1, y1, x2, y2 = box
    width = max(1, x2 - x1)
    height = max(1, y2 - y1)
    gradient = Image.new("RGB", (width, height), start_color)
    gd = ImageDraw.Draw(gradient)
    s = tuple(int(start_color[i:i+2], 16) for i in (1, 3, 5))
    e = tuple(int(end_color[i:i+2], 16) for i in (1, 3, 5))
    for x in range(width):
        t = x / max(1, width - 1)
        c = tuple(round(s[i] + (e[i] - s[i]) * t) for i in range(3))
        gd.line((x, 0, x, height), fill=c)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    image.paste(gradient, (x1, y1), mask)


def center_text_xy(draw: ImageDraw.ImageDraw, text: str, font: Any, center: tuple[float, float]) -> tuple[float, float]:
    """Return a draw.text origin that visually centers the glyph bbox on ``center``."""
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    return (
        center[0] - width / 2 - bbox[0],
        center[1] - height / 2 - bbox[1],
    )



def format_rank_updated_label(updated_at: Any, *, now: datetime | None = None) -> str:
    """Format ranking update time compactly without hiding an older date."""
    text = str(updated_at or "").strip()
    if not text:
        return "暂无记录"
    try:
        moment = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return f"更新 {text}"
    current = now or datetime.now()
    if moment.date() == current.date():
        return f"更新 {moment:%H:%M}"
    return f"更新 {moment:%m-%d %H:%M}"


def build_rank_footer_stats(summary: dict[str, Any], rank_limit: int) -> list[tuple[str, str]]:
    """Build the three compact footer stats shown on ranking cards."""
    total = int(summary.get("total_pokes", 0))
    unique = int(summary.get("unique_users", 0))
    daily = bool(summary.get("is_daily", False))
    global_scope = bool(summary.get("is_global_scope", False))
    total_label = "今日被戳" if daily else "历史被戳"

    if not global_scope:
        rank = summary.get("group_total_rank")
        group_count = int(summary.get("group_total_groups", 0))
        rank_value = (
            f"第 {int(rank)} / {group_count}"
            if rank is not None and group_count > 0
            else "暂无排名"
        )
        third = ("群排名", rank_value)
    else:
        shown = min(len(summary.get("entries", [])), max(1, int(rank_limit)))
        third = ("榜单显示", f"{shown} 人")

    return [
        (total_label, f"{total} 次"),
        ("参与用户", f"{unique} 人"),
        third,
    ]

def render_poke_rank_image(
    output_path: str | os.PathLike[str],
    *,
    title: str,
    subtitle: str,
    summary: dict[str, Any],
    rank_limit: int = 10,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = list(summary.get("entries", []))[: max(1, int(rank_limit))]
    target_user_id = summary.get("target_user_id")
    target_user_count = summary.get("target_user_count")
    display_names = summary.get("display_names") or {}

    width = 960
    top_margin = 32
    header_h = 150
    row_h = 76
    list_h = max(160, 28 + len(entries) * row_h)
    query_h = 86 if target_user_id else 0
    footer_h = 128
    gap = 20
    height = top_margin + header_h + gap + list_h + gap + query_h + (gap if query_h else 0) + footer_h + 32

    image = Image.new("RGB", (width, height), "#FFF8FC")
    draw = ImageDraw.Draw(image)

    bg_top = (255, 247, 252)
    bg_bottom = (246, 243, 255)
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(round(bg_top[i] + (bg_bottom[i] - bg_top[i]) * t) for i in range(3))
        draw.line((0, y, width, y), fill=color)

    draw.ellipse((790, -45, 1015, 180), fill="#FDE3EF")
    draw.ellipse((-70, height - 180, 130, height + 20), fill="#EDE7FF")

    left, right = 34, width - 34
    header_box = (left, top_margin, right, top_margin + header_h)
    _rounded_gradient(image, header_box, "#FF84AF", "#B89CFF", 30)

    title_font = _load_font(38, True)
    subtitle_font = _load_font(21, False)
    small_font = _load_font(18, False)
    rank_font = _load_font(21, True)
    body_font = _load_font(25, True)
    count_font = _load_font(27, True)
    stat_value_font = _load_font(29, True)
    stat_label_font = _load_font(17, False)

    draw.text((left + 34, top_margin + 28), title, font=title_font, fill="white")
    draw.text((left + 36, top_margin + 84), subtitle, font=subtitle_font, fill="#FFF7FB")
    updated_text = format_rank_updated_label(summary.get("updated_at"))
    updated_box = draw.textbbox((0, 0), updated_text, font=small_font)
    draw.text((right - (updated_box[2] - updated_box[0]) - 32, top_margin + 105), updated_text, font=small_font, fill="#FFF4FA")

    list_top = top_margin + header_h + gap
    draw.rounded_rectangle((left + 3, list_top + 6, right + 3, list_top + list_h + 6), radius=28, fill="#E8DCE8")
    draw.rounded_rectangle((left, list_top, right, list_top + list_h), radius=28, fill="#FFFFFF")

    if not entries:
        empty_font = _load_font(25, True)
        hint_font = _load_font(20, False)
        draw.text((left + 48, list_top + 50), "还没有 Poke 记录", font=empty_font, fill="#493E54")
        draw.text((left + 48, list_top + 94), "快来戳一戳，让排行榜热闹起来吧～", font=hint_font, fill="#8C8194")
    else:
        max_count = max(int(count) for _, count in entries) or 1
        medal_fills = ("#F8C95F", "#C8CFDE", "#DCA47B")
        for index, (user_id, count) in enumerate(entries, start=1):
            y = list_top + 18 + (index - 1) * row_h
            row_box = (left + 20, y, right - 20, y + 60)
            row_fill = "#FFF4F8" if index <= 3 else "#FAF8FC"
            draw.rounded_rectangle(row_box, radius=19, fill=row_fill)

            badge_x = left + 42
            badge_y = y + 10
            badge_fill = medal_fills[index - 1] if index <= 3 else "#E9E4EE"
            draw.ellipse((badge_x, badge_y, badge_x + 40, badge_y + 40), fill=badge_fill)
            rank_text = str(index)
            rank_xy = center_text_xy(
                draw, rank_text, rank_font, (badge_x + 20, badge_y + 20)
            )
            draw.text(rank_xy, rank_text, font=rank_font, fill="#4B4051")

            display_name = privacy_safe_label(user_id, display_names.get(str(user_id)))
            display_name = _truncate_label(display_name)
            draw.text((left + 102, y + 13), display_name, font=body_font, fill="#3B3242")

            bar_left = left + 420
            bar_right = right - 150
            bar_y = y + 24
            draw.rounded_rectangle((bar_left, bar_y, bar_right, bar_y + 12), radius=6, fill="#EEE8F1")
            ratio = max(0.06, int(count) / max_count)
            fill_right = bar_left + int((bar_right - bar_left) * ratio)
            draw.rounded_rectangle((bar_left, bar_y, fill_right, bar_y + 12), radius=6, fill="#F29BB9" if index <= 3 else "#C2AFE9")

            count_text = f"{int(count)} 次"
            cb = draw.textbbox((0, 0), count_text, font=count_font)
            draw.text((right - 44 - (cb[2] - cb[0]), y + 11), count_text, font=count_font, fill="#F05F95")

    current_y = list_top + list_h + gap

    if target_user_id:
        draw.rounded_rectangle((left, current_y, right, current_y + query_h), radius=24, fill="#FFF0F6")
        draw.text((left + 30, current_y + 20), "个人查询", font=small_font, fill="#A95E7A")
        target_name = privacy_safe_label(target_user_id, display_names.get(str(target_user_id)))
        target_name = _truncate_label(target_name, 16)
        query_text = f"{target_name}  累计 {int(target_user_count or 0)} 次"
        qb = draw.textbbox((0, 0), query_text, font=body_font)
        draw.text((right - 30 - (qb[2] - qb[0]), current_y + 27), query_text, font=body_font, fill="#D94E83")
        current_y += query_h + gap

    draw.rounded_rectangle((left, current_y, right, current_y + footer_h), radius=26, fill="#FFFFFF")
    stats = build_rank_footer_stats(summary, rank_limit)
    col_w = (right - left - 44) // 3
    for i, (label, value_text) in enumerate(stats):
        x = left + 22 + i * col_w
        if i:
            draw.line((x - 12, current_y + 26, x - 12, current_y + footer_h - 26), fill="#EFE8F1", width=2)
        draw.text((x + 12, current_y + 27), label, font=stat_label_font, fill="#918697")
        draw.text((x + 12, current_y + 60), value_text, font=stat_value_font, fill="#4A3E52")

    temp_output = output.with_name(output.stem + ".tmp" + output.suffix)
    image.save(temp_output, format="PNG", optimize=True)
    os.replace(temp_output, output)
    return output
