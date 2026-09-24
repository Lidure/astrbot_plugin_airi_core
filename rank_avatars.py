from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageOps


def _circle_mask(size: tuple[int, int]) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size[0] - 1, size[1] - 1), fill=255)
    return mask


def _draw_placeholder(canvas: Image.Image, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    width = max(1, x2 - x1)
    height = max(1, y2 - y1)
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((x1, y1, x2 - 1, y2 - 1), fill="#E9DDF7", outline="#D8C6ED", width=2)
    cx = x1 + width / 2
    head_r = min(width, height) * 0.14
    head_y = y1 + height * 0.37
    draw.ellipse(
        (cx - head_r, head_y - head_r, cx + head_r, head_y + head_r),
        fill="#A990C6",
    )
    body_w = min(width, height) * 0.42
    body_h = min(width, height) * 0.25
    draw.rounded_rectangle(
        (
            cx - body_w / 2,
            y1 + height * 0.57,
            cx + body_w / 2,
            y1 + height * 0.57 + body_h,
        ),
        radius=max(2, int(body_h / 2)),
        fill="#A990C6",
    )


def paste_circular_avatar(
    canvas: Image.Image,
    avatar_path: str | Path | None,
    box: tuple[int, int, int, int],
) -> bool:
    """Paste a circular avatar; draw a placeholder when loading fails."""
    x1, y1, x2, y2 = map(int, box)
    width = max(1, x2 - x1)
    height = max(1, y2 - y1)
    try:
        if not avatar_path:
            raise OSError("missing avatar")
        with Image.open(avatar_path) as source:
            source.load()
            avatar = ImageOps.fit(
                source.convert("RGB"),
                (width, height),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
        mask = _circle_mask((width, height))
        canvas.paste(avatar, (x1, y1), mask)
        ImageDraw.Draw(canvas).ellipse(
            (x1, y1, x2 - 1, y2 - 1),
            outline="#FFFFFF",
            width=2,
        )
        return True
    except (OSError, ValueError):
        _draw_placeholder(canvas, (x1, y1, x2, y2))
        return False
