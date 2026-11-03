"""QQ 空间好友说说自动点赞（SnowLuma / OneBot v11）。

独立于 Airi Core 其他功能；默认关闭。仅在已获取登录 QQ 的 OneBot 客户端后启动。
QQ 空间接口并非官方稳定 API；好友动态覆盖率无法保证。

QQ 空间 CGI 的请求字段参考 cube-lover/astrbot_qzone_auto_like (MIT)：
Copyright (c) 2026 cube-lover；见 THIRD_PARTY_NOTICES.md。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

_MOOD_RE = re.compile(
    r"https?[:\\/]+user\.qzone\.qq\.com[:\\/]+(\d+)[:\\/]+mood[:\\/]+([0-9a-fA-F]+)",
    re.IGNORECASE,
)
_COOKIE_RE = re.compile(r"(?:^|;\s*)([a-zA-Z_]+)=([^;]*)")
_LOGIN_HINTS = ("not log in", "need login", "验证码", "安全验证", "请先登录")


def parse_mood_links(body: str) -> list[tuple[str, str]]:
    """只提取 QQ 空间 mood 链接，不解析用户文字内容。"""
    return list(dict.fromkeys((qq, fid.lower()) for qq, fid in _MOOD_RE.findall(body)))


def gtk_from_cookie(cookie: str) -> int:
    values = dict(_COOKIE_RE.findall(cookie or ""))
    skey = values.get("p_skey") or values.get("skey") or values.get("media_p_skey")
    if not skey:
        raise ValueError("QQ 空间 Cookie 缺少 p_skey/skey")
    value = 5381
    for ch in skey:
        value += (value << 5) + ord(ch)
    return value & 0x7FFFFFFF


def like_succeeded(status: int, text: str) -> bool:
    if status != 200:
        return False
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        # 某些 CGI 返回 JS 文本/JSONP；仅解析明确的 code 数值。
        match = re.search(r"""["']?code["']?\s*:\s*(-?\d+)""", text or "")
        if not match:
            return False
        return int(match.group(1)) == 0 and "记录成功" not in text
    return (
        isinstance(result, dict)
        and result.get("code") == 0
        and "记录成功" not in str(result.get("message") or "")
    )


def _http_get(url: str, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return resp.status, resp.read(1_500_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2048).decode("utf-8", "replace")


def _http_post(
    url: str, headers: dict[str, str], fields: dict[str, str]
) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, headers=headers, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return resp.status, resp.read(8192).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2048).decode("utf-8", "replace")


class QzoneAccessError(RuntimeError):
    """需要暂停而不是持续重试的登录态、权限或风控错误。"""


class QzoneAutoLike:
    def __init__(
        self,
        state_path: Path,
        *,
        logger: Any = None,
        bot_qq: str = "",
        enabled: bool = False,
        dry_run: bool = True,
        interval_sec: int = 300,
        friend_batch_size: int = 8,
        feed_count: int = 30,
        max_likes_per_cycle: int = 3,
        delay_min_sec: int = 15,
        delay_max_sec: int = 30,
        http_get: Callable = _http_get,
        http_post: Callable = _http_post,
        sleep: Callable = asyncio.sleep,
    ) -> None:
        self.state_path = Path(state_path)
        self.logger = logger
        self.expected_qq = str(bot_qq or "").strip()
        self.enabled = bool(enabled)
        self.dry_run = bool(dry_run)
        self.interval = max(120, int(interval_sec))
        self.batch_size = max(0, min(20, int(friend_batch_size)))
        self.feed_count = max(10, min(60, int(feed_count)))
        self.max_likes = max(1, min(10, int(max_likes_per_cycle)))
        self.delay_min = max(0, min(120, int(delay_min_sec)))
        self.delay_max = max(self.delay_min, min(180, int(delay_max_sec)))
        self._get = http_get
        self._post = http_post
        self._sleep = sleep
        self._bot: Any = None
        self._self_qq = ""
        self._task: asyncio.Task | None = None
        self._scan_lock = asyncio.Lock()
        self._friends: set[str] = set()
        self._friend_cache_at = 0.0
        self._suspended_until = 0.0
        self._last_scan = 0.0
        self._last_error = ""
        self._liked = 0
        self._failed = 0
        self._dry_count = 0
        self._baseline_count = 0
        self._state = self._load()

    def _log(self, level: str, text: str) -> None:
        method = getattr(self.logger, level, None) if self.logger else None
        if callable(method):
            method("[QzoneLike] %s", text)

    def _load(self) -> dict:
        try:
            with self.state_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict) and isinstance(data.get("seen"), dict):
                return {
                    "seen": data["seen"],
                    "initialized": list(data.get("initialized", [])),
                    "cursor": int(data.get("cursor", 0)),
                    "account": str(data.get("account", "")),
                }
        except (OSError, ValueError, TypeError):
            pass
        return {"seen": {}, "initialized": [], "cursor": 0, "account": ""}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        seen = self._state["seen"]
        if len(seen) > 30000:
            for key, _ in sorted(seen.items(), key=lambda row: row[1])[:len(seen) - 25000]:
                seen.pop(key, None)
        temp = self.state_path.with_suffix(".tmp")
        try:
            with temp.open("w", encoding="utf-8") as file:
                json.dump(self._state, file, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp, self.state_path)
        finally:
            if temp.exists():
                temp.unlink()

    def attach_bot(self, bot: Any, self_qq: str) -> bool:
        qq = str(self_qq or "").strip()
        if not bot or not qq.isdecimal() or (self.expected_qq and qq != self.expected_qq):
            return False
        if self._self_qq and self._self_qq != qq:
            return False
        self._bot = bot
        self._self_qq = qq
        if self._state["account"] and self._state["account"] != qq:
            # 不可将其他登录账号的去重记录与首次扫描状态混用。
            self._state = {"seen": {}, "initialized": [], "cursor": 0, "account": qq}
            self._save()
        elif not self._state["account"]:
            self._state["account"] = qq
            self._save()
        if self.enabled:
            self.start()
        return True

    def start(self) -> None:
        self.enabled = True
        if self._bot is not None and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._worker(), name="airi-qzone-liker")

    def stop(self) -> None:
        self.enabled = False
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def close(self) -> None:
        self.stop()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def status(self) -> str:
        active = self._task is not None and not self._task.done()
        wait = max(0, int(self._suspended_until - time.time()))
        return (
            f"QQ空间自动点赞：{'已开启' if self.enabled else '已关闭'}"
            f" / {'只读测试' if self.dry_run else '实际点赞'}\n"
            f"任务：{'运行中' if active else '未运行'}；好友数：{len(self._friends)}\n"
            f"已索引动态：{len(self._state['seen'])}；已初始化来源：{len(self._state['initialized'])}\n"
            f"本次运行点赞：{self._liked}；失败：{self._failed}；预演：{self._dry_count}\n"
            f"上次扫描：{time.strftime('%m-%d %H:%M:%S', time.localtime(self._last_scan)) if self._last_scan else '尚未扫描'}"
            f"{'；暂停剩余 ' + str(wait) + ' 秒' if wait else ''}"
            f"{'；错误：' + self._last_error if self._last_error else ''}"
        )

    async def _call_bot(self, action: str, **kwargs) -> Any:
        if self._bot is None:
            raise RuntimeError("尚未连接 OneBot")
        method = getattr(self._bot, action, None)
        if not callable(method):
            method = getattr(self._bot, "call_action", None)
            if callable(method):
                result = await method(action, **kwargs)
            else:
                raise RuntimeError("OneBot 不支持 " + action)
        else:
            result = await method(**kwargs)
        if isinstance(result, dict):
            if result.get("status") == "failed" or result.get("retcode", 0) not in (0, None):
                raise RuntimeError("OneBot " + action + " 返回失败")
            return result.get("data", result)
        return result

    async def _refresh_friends(self) -> None:
        if self._friends and time.time() - self._friend_cache_at < 3600:
            return
        result = await self._call_bot("get_friend_list")
        if not isinstance(result, list):
            raise RuntimeError("get_friend_list 未返回好友列表")
        friends = set()
        for item in result:
            qq = str(item.get("user_id", "")).strip() if isinstance(item, dict) else ""
            if qq.isdecimal() and qq != self._self_qq:
                friends.add(qq)
        if not friends:
            raise RuntimeError("好友列表为空，跳过扫描")
        self._friends = friends
        self._friend_cache_at = time.time()

    def _headers(self, cookie: str) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/144.0.0.0 Safari/537.36",
            "Cookie": cookie,
            "Referer": "https://user.qzone.qq.com/" + self._self_qq + "/main",
            "Origin": "https://user.qzone.qq.com",
            "Accept": "*/*",
        }

    def _feed_url(self, target: str | None, gtk: int) -> str:
        base = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/"
        if target is None:
            return (
                base + "feeds3_html_more?"
                + urllib.parse.urlencode({
                    "uin": self._self_qq, "scope": 0, "view": 1, "flag": 1,
                    "refresh": 1, "count": self.feed_count, "outputhtmlfeed": 1, "g_tk": gtk,
                })
            )
        return (
            base + "feeds_html_act_all?"
            + urllib.parse.urlencode({
                "uin": self._self_qq, "hostuin": target, "scope": 0,
                "filter": "all", "flag": 1, "refresh": 0, "firstGetGroup": 0,
                "mixnocache": 0, "scene": 0, "start": 0, "count": self.feed_count,
                "sidomain": "qzonestyle.gtimg.cn", "useutf8": 1,
                "outputhtmlfeed": 1, "refer": 2, "r": random.random(), "g_tk": gtk,
            })
        )

    def _sources(self) -> list[tuple[str, str | None]]:
        friends = sorted(self._friends)
        result: list[tuple[str, str | None]] = [("feed", None)]
        if not friends or not self.batch_size:
            return result
        cursor = self._state["cursor"] % len(friends)
        count = min(self.batch_size, len(friends))
        for step in range(count):
            friend = friends[(cursor + step) % len(friends)]
            result.append(("friend:" + friend, friend))
        self._state["cursor"] = (cursor + count) % len(friends)
        return result

    async def _send_like(
        self, author: str, fid: str, gtk: int, headers: dict[str, str]
    ) -> bool:
        mood = f"https://user.qzone.qq.com/{author}/mood/{fid}"
        endpoint = (
            "https://h5.qzone.qq.com/proxy/domain/w.qzone.qq.com/"
            f"cgi-bin/likes/internal_dolike_app?g_tk={gtk}"
        )
        form = {
            "qzreferrer": (
                "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/"
                "cgi-bin/feeds/feeds_html_module?g_iframeUser=1"
                f"&i_uin={author}&i_login_uin={self._self_qq}"
                "&mode=4&refer=2"
            ),
            "opuin": self._self_qq, "unikey": mood, "curkey": mood,
            "from": "1", "appid": "311", "typeid": "0",
            "abstime": str(int(time.time())), "fid": fid,
            "active": "0", "fupdate": "1",
        }
        response_status, response_text = await asyncio.to_thread(
            self._post, endpoint, headers, form
        )
        if response_status in (401, 403, 429) or any(
            hint in response_text.lower() for hint in _LOGIN_HINTS
        ):
            raise QzoneAccessError("点赞接口要求重新登录或触发安全验证")
        return like_succeeded(response_status, response_text)

    async def scan_once(self) -> dict[str, int]:
        """一个周期：好友动态流 + 轮询若干好友空间；每个来源首次扫描只建索引。"""
        if self._bot is None or not self._self_qq:
            raise RuntimeError("尚未从 OneBot 消息捕获机器人连接")
        async with self._scan_lock:
            if time.time() < self._suspended_until:
                return {"new": 0, "liked": 0, "baseline": 0}
            result = {"new": 0, "liked": 0, "baseline": 0}
            try:
                await self._refresh_friends()
                cookie_data = await self._call_bot("get_cookies", domain="user.qzone.qq.com")
                cookie = (
                    str(cookie_data.get("cookies") or "")
                    if isinstance(cookie_data, dict)
                    else str(cookie_data or "")
                )
                gtk = gtk_from_cookie(cookie)
                headers = self._headers(cookie)
                sources = self._sources()
                initialized = set(self._state["initialized"])
                for name, target in sources:
                    url = self._feed_url(target, gtk)
                    status, body = await asyncio.to_thread(self._get, url, headers)
                    if status in (401, 403, 429) or any(
                        hint in body.lower() for hint in _LOGIN_HINTS
                    ):
                        raise QzoneAccessError("空间动态接口需要重新登录或安全验证")
                    if status != 200:
                        raise RuntimeError(f"QQ 空间动态读取 HTTP {status}")
                    moods = [
                        (author, fid) for author, fid in parse_mood_links(body)
                        if author in self._friends and (target is None or author == target)
                    ]
                    # 接口无有效链接可能是权限受限或返回格式更改，不能标记初始化完成。
                    if not moods:
                        continue
                    if name not in initialized:
                        for author, fid in moods:
                            key = author + ":" + fid
                            if key not in self._state["seen"]:
                                self._state["seen"][key] = time.time()
                                result["baseline"] += 1
                        initialized.add(name)
                        self._state["initialized"] = sorted(initialized)
                        self._save()
                        continue
                    for author, fid in moods:
                        key = author + ":" + fid
                        if key in self._state["seen"]:
                            continue
                        if result["new"] >= self.max_likes:
                            break
                        # 先持久化尝试记录，崩溃/重启时不会把同一条动态再次点赞。
                        self._state["seen"][key] = time.time()
                        self._save()
                        result["new"] += 1
                        if self.dry_run:
                            self._dry_count += 1
                            continue
                        await self._sleep(random.uniform(self.delay_min, self.delay_max))
                        success = await self._send_like(author, fid, gtk, headers)
                        if success:
                            self._liked += 1
                            result["liked"] += 1
                        else:
                            self._failed += 1
                    if result["new"] >= self.max_likes:
                        break
                self._save()
                self._last_error = ""
            except QzoneAccessError as exc:
                # 不进行验证码绕过；暂停至少一小时。
                self._suspended_until = time.time() + 3600
                self._last_error = str(exc)
                self._log("warning", self._last_error)
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:100]}"
                self._log("warning", self._last_error)
            finally:
                self._last_scan = time.time()
                self._baseline_count += result["baseline"]
            return result

    async def _worker(self) -> None:
        try:
            while self.enabled:
                summary = await self.scan_once()
                self._log(
                    "info",
                    "本轮结束：新增=%d 点赞=%d 初次索引=%d 已知好友=%d"
                    % (summary["new"], summary["liked"], summary["baseline"], len(self._friends)),
                )
                await self._sleep(self.interval)
        except asyncio.CancelledError:
            pass
