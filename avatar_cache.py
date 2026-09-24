from __future__ import annotations

import io
import os
import time
import urllib.request
from pathlib import Path
from typing import Callable

from PIL import Image

QQ_AVATAR_TEMPLATE = "https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
DEFAULT_TTL_SECONDS = 24 * 60 * 60
MAX_AVATAR_BYTES = 2 * 1024 * 1024


def qq_avatar_url(user_id: str) -> str:
    return QQ_AVATAR_TEMPLATE.format(user_id=str(user_id))


def _download_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AstrBot-AiriCore/1.0"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        data = response.read(MAX_AVATAR_BYTES + 1)
    if len(data) > MAX_AVATAR_BYTES:
        raise ValueError("avatar response too large")
    return data


class QqAvatarCache:
    def __init__(
        self,
        cache_dir: str | os.PathLike[str],
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        fetcher: Callable[[str], bytes] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.fetcher = fetcher or _download_bytes

    @staticmethod
    def _valid_user_id(user_id: str) -> bool:
        text = str(user_id or "").strip()
        return bool(text) and text.isdigit()

    @staticmethod
    def _is_valid_image(path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            with Image.open(path) as image:
                image.verify()
            return True
        except (OSError, ValueError):
            return False

    def _is_fresh(self, path: Path) -> bool:
        if not self._is_valid_image(path):
            return False
        try:
            return (time.time() - path.stat().st_mtime) <= self.ttl_seconds
        except OSError:
            return False

    def get(self, user_id: str) -> str | None:
        user_id = str(user_id or "").strip()
        if not self._valid_user_id(user_id):
            return None

        path = self.cache_dir / f"{user_id}.png"
        if self._is_fresh(path):
            return str(path)

        stale_valid = self._is_valid_image(path)
        try:
            payload = self.fetcher(qq_avatar_url(user_id))
            if not isinstance(payload, (bytes, bytearray)) or not payload:
                raise ValueError("empty avatar response")
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                normalized = image.convert("RGB")
            temp = path.with_suffix(".tmp.png")
            normalized.save(temp, format="PNG", optimize=True)
            os.replace(temp, path)
            return str(path)
        except Exception:
            return str(path) if stale_valid else None
