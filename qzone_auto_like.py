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
_LOGIN_HINTS = ("not log in", "need login", "captcha", "\"code\":-4001", "<title>安全验证</title>", "<title>验证码</title>", "请先登录")


def parse_mood_links(body: str) -> list[tuple[str, str]]:
    """只提取 QQ 空间 mood 链接，不解析用户文字内容。"""
    return list(dict.fromkeys((qq, fid.lower()) for qq, fid in _MOOD_RE.findall(body)))


_FEED_DATA_TAG = re.compile(r'<[^>]*\bname\s*=\s*["\']feed_data["\'][^>]*>', re.I)
_FEED_ATTR = re.compile(r'\bdata-(uin|tid|abstime)\s*=\s*(["\'])(.*?)\2', re.I | re.S)


def parse_mood_publish_times(body: str) -> dict[tuple[str, str], int]:
    """从 feed_data 精确提取 (作者, 说说 ID) 对应的发布时间。

    只匹配页面真实 mood 链接。无法确认的时间保持未知，不靠位置猜测。
    """
    normalized = re.sub(
        r"\\x([0-9a-fA-F]{2})",
        lambda m: chr(int(m.group(1), 16)),
        body or "",
    ).replace("\\/", "/")
    visible = set(parse_mood_links(normalized))
    times: dict[tuple[str, str], int] = {}
    for tag in _FEED_DATA_TAG.findall(normalized):
        attrs = {name.lower(): value for name, _, value in _FEED_ATTR.findall(tag)}
        author, fid = attrs.get("uin", ""), attrs.get("tid", "").lower()
        if (author, fid) not in visible:
            continue
        try:
            published = int(attrs.get("abstime", ""))
        except (TypeError, ValueError):
            continue
        if 1_400_000_000 <= published <= int(time.time()) + 60:
            times[(author, fid)] = published
    return times


def parse_like_state(status: int, text: str) -> bool | None:
    """Return True=liked, False=not liked, None=invalid/unknown."""
    if status != 200:
        return None
    raw = (text or "").strip()
    if not raw.startswith("{"):
        start, end = raw.find("("), raw.rfind(")")
        if start < 0 or end <= start:
            return None
        raw = raw[start + 1:end]
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(result, dict) or result.get("code") != 0:
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    state = str(data.get("is_dolike"))
    return state == "1" if state in ("0", "1") else None


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


def like_confirmed(status: int, text: str) -> bool:
    """Only trust the separate likes-list API's is_dolike flag.

    An internal_dolike_app response with code=0 does NOT by itself prove
    that the selected mood was liked (confirmed by a SnowLuma live probe).
    """
    if status != 200:
        return False
    body = (text or "").strip()
    if not body.startswith("{"):
        open_paren, close_paren = body.find("("), body.rfind(")")
        if open_paren < 0 or close_paren <= open_paren:
            return False
        body = body[open_paren + 1:close_paren]
    try:
        result = json.loads(body)
    except (ValueError, TypeError):
        return False
    if not isinstance(result, dict) or result.get("code") != 0:
        return False
    data = result.get("data")
    return isinstance(data, dict) and str(data.get("is_dolike")) == "1"


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
                    "started_at": float(data.get("started_at") or min(
                        (float(v) for v in data["seen"].values()
                         if isinstance(v, (int, float))),
                        default=time.time(),
                    )),
                    "resolved": dict(data.get("resolved", {})),
                    "pending": self._parse_pending(data.get("pending", [])),
                }
        except (OSError, ValueError, TypeError):
            pass
        return {
            "seen": {}, "initialized": [], "cursor": 0, "account": "",
            "started_at": time.time(), "resolved": {}, "pending": [],
        }


    @staticmethod
    def _parse_pending(data: Any) -> list[dict]:
        """Validate and deduplicate queue entries loaded from a prior restart."""
        if not isinstance(data, list):
            return []
        entries: list[dict] = []
        ids: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            author, fid = str(item.get("author", "")), str(item.get("fid", "")).lower()
            if not author.isdecimal() or not re.fullmatch(r"[a-f0-9]+", fid):
                continue
            key = f"{author}:{fid}"
            if key in ids:
                continue
            ids.add(key)
            try:
                published = int(item.get("published_at") or 0)
                queued = float(item.get("queued_at") or time.time())
                attempts = max(0, min(3, int(item.get("attempts", 0))))
                next_retry = max(0.0, float(item.get("next_retry_at", 0)))
            except (ValueError, TypeError, OverflowError):
                continue
            entries.append({
                "author": author, "fid": fid,
                "published_at": published, "queued_at": queued,
                "attempts": attempts, "next_retry_at": next_retry,
                "legacy": bool(item.get("legacy", False)),
                "dry_run_only": bool(item.get("dry_run_only", False)),
            })
        return entries

    def _enqueue(self, author: str, fid: str, published: int | None,
                 legacy: bool = False) -> bool:
        """Atomically persist a new candidate before any likes are attempted."""
        key = f"{author}:{fid}"
        if key in self._state["resolved"]:
            return False
        if any(p["author"] == author and p["fid"] == fid
               for p in self._state["pending"]):
            return False
        # A full queue never silently marks an unprocessed candidate as seen.
        if len(self._state["pending"]) >= 2000:
            self._log("warning", "待点赞队列已满（2000条），暂停加入新任务")
            return False
        self._state["pending"].append({
            "author": author, "fid": fid,
            "published_at": int(published or 0),
            "queued_at": time.time(),
            "attempts": 0, "next_retry_at": 0.0,
            "legacy": bool(legacy),
            "dry_run_only": bool(self.dry_run),
        })
        self._state["seen"][key] = time.time()
        self._save()
        return True

    async def _drain_pending(
        self, gtk: int, headers: dict[str, str], summary: dict[str, int]
    ) -> None:
        """Process at most max_likes queued items, oldest first.

        Read-only status check occurs before each POST. Failures remain queued
        with bounded exponential backoff and a 3-POST safety ceiling.
        """
        pending = self._state["pending"]
        # Sort by discovery time, rather than feed/source discovery order.
        pending.sort(key=lambda item: (item["queued_at"], item["published_at"]))
        processed = 0
        read_checks = 0
        max_reads = max(5, self.max_likes * 3)
        for item in list(pending):
            if processed >= self.max_likes:
                break
            if item not in pending:
                continue
            if item["attempts"] >= 3:
                continue
            if time.time() < item["next_retry_at"]:
                continue
            key = item["author"] + ":" + item["fid"]
            author, fid = item["author"], item["fid"]

            if author not in self._friends or author == self._self_qq:
                # Removed from friend list: do not like a non-friend.
                pending.remove(item)
                self._state["resolved"][key] = time.time()
                self._save()
                summary["skipped"] += 1
                continue

            if self.dry_run or item.get("dry_run_only", False):
                # Dry-run discoveries remain historical when switching to live.
                pending.remove(item)
                self._state["resolved"][key] = time.time()
                self._save()
                self._dry_count += 1
                summary["dry_run"] += 1
                processed += 1
                continue

            if read_checks >= max_reads:
                break
            read_checks += 1
            current = await self._read_like_state(author, fid, gtk, headers)
            if current is None:
                # Unavailable/ambiguous status is not permission to POST.
                item["next_retry_at"] = time.time() + max(300, self.interval)
                self._save()
                summary["deferred"] += 1
                continue
            if current:
                pending.remove(item)
                self._state["resolved"][key] = time.time()
                self._save()
                summary["already_liked"] += 1
                processed += 1
                continue

            # Mark the attempt before sending so a crash cannot issue an
            # unrecorded POST. A later retry must first query current state.
            item["attempts"] += 1
            item["next_retry_at"] = time.time() + max(
                300, self.interval, 300 * 3 ** (item["attempts"] - 1)
            )
            self._save()
            processed += 1
            summary["attempted"] += 1
            if item["legacy"]:
                summary["recovered"] += 1

            await self._sleep(random.uniform(self.delay_min, self.delay_max))
            try:
                success = await self._send_like(author, fid, gtk, headers)
            except QzoneAccessError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:100]}"
                self._log("warning", "队列点赞请求失败，已保留待重试任务")
                self._failed += 1
                summary["failed"] += 1
                continue
            if success:
                pending.remove(item)
                self._state["resolved"][key] = time.time()
                self._liked += 1
                summary["liked"] += 1
            else:
                self._failed += 1
                summary["failed"] += 1
                self._log("warning", "点赞状态未确认，队列任务保留等待下轮核验")
            self._save()
        summary["pending"] = len(pending)

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        seen = self._state["seen"]
        if len(seen) > 30000:
            for key, _ in sorted(seen.items(), key=lambda row: row[1])[:len(seen) - 25000]:
                seen.pop(key, None)
        resolved = self._state["resolved"]
        if len(resolved) > 30000:
            for key, _ in sorted(resolved.items(), key=lambda row: row[1])[:len(resolved) - 25000]:
                resolved.pop(key, None)
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
            self._state = {
                "seen": {}, "initialized": [], "cursor": 0, "account": qq,
                "started_at": time.time(), "resolved": {}, "pending": [],
            }
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
            f"待点赞队列：{len(self._state['pending'])} 条；"
            f"重试已暂停：{sum(int(item['attempts']) >= 3 for item in self._state['pending'])} 条\n"
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
        # Use the exact HTTP mood-key scheme used by the QQ Space feed like button.
        mood = f"http://user.qzone.qq.com/{author}/mood/{fid}"
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
        if not like_succeeded(response_status, response_text):
            return False

        # The POST may return code=0 while no like was actually applied.
        # Confirm with a read-only likes query before counting success.
        await self._sleep(1)
        query = urllib.parse.urlencode({
            "uin": self._self_qq,
            "unikey": mood + ".1",
            "begin_uin": 0,
            "query_count": 60,
            "if_first_page": 1,
            "g_tk": gtk,
        })
        verify_url = (
            "https://h5.qzone.qq.com/proxy/domain/users.qzone.qq.com/"
            "cgi-bin/likes/get_like_list_app?" + query
        )
        verify_status, verify_text = await asyncio.to_thread(
            self._get, verify_url, headers
        )
        if verify_status in (401, 403, 429):
            raise QzoneAccessError("QQ 空间点赞核验接口受到访问限制")
        confirmed = like_confirmed(verify_status, verify_text)
        if not confirmed:
            self._log(
                "warning",
                "点赞接口返回成功代码，但点赞状态未确认；不会重复点赞同一动态",
            )
        return confirmed

    async def _read_like_state(
        self, author: str, fid: str, gtk: int, headers: dict[str, str]
    ) -> bool | None:
        # Legacy records may already have been liked by a previous plugin version.
        # Always verify these before sending another POST.
        mood = f"http://user.qzone.qq.com/{author}/mood/{fid}"
        query = urllib.parse.urlencode({
            "uin": self._self_qq, "unikey": mood + ".1",
            "begin_uin": 0, "query_count": 60,
            "if_first_page": 1, "g_tk": gtk,
        })
        url = (
            "https://h5.qzone.qq.com/proxy/domain/users.qzone.qq.com/"
            "cgi-bin/likes/get_like_list_app?" + query
        )
        status, text = await asyncio.to_thread(self._get, url, headers)
        if status in (401, 403, 429):
            raise QzoneAccessError("QQ 空间点赞状态查询被限制，请稍后再试")
        return parse_like_state(status, text)

    async def scan_once(self) -> dict[str, int]:
        """Discover and durably queue *all* eligible new posts, then like a batch."""
        if self._bot is None or not self._self_qq:
            raise RuntimeError("尚未从 OneBot 消息捕获机器人连接")
        async with self._scan_lock:
            result = {
                "new": 0, "queued": 0, "attempted": 0, "liked": 0,
                "baseline": 0, "recovered": 0, "already_liked": 0,
                "failed": 0, "deferred": 0, "skipped": 0, "dry_run": 0,
                "pending": len(self._state["pending"]),
            }
            if time.time() < self._suspended_until:
                return result
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
                started = float(self._state["started_at"])

                # Phase 1: scan every source, collect all candidates.
                # Do not stop discovering when the per-cycle like limit is met.
                for name, target in sources:
                    url = self._feed_url(target, gtk)
                    status, body = await asyncio.to_thread(self._get, url, headers)
                    if status in (401, 403, 429) or any(
                        hint in body.lower() for hint in _LOGIN_HINTS
                    ):
                        raise QzoneAccessError("空间动态接口需要重新登录或安全验证")
                    if status != 200:
                        raise RuntimeError(f"QQ 空间动态读取 HTTP {status}")

                    published_times = parse_mood_publish_times(body)
                    moods = [
                        (author, fid) for author, fid in parse_mood_links(body)
                        if author in self._friends and (target is None or author == target)
                    ]
                    valid_empty_feed = any(
                        marker in body for marker in ("friend_data", "host_data", "data_items")
                    )
                    if not moods and not valid_empty_feed:
                        continue
                    first_source_scan = name not in initialized
                    if first_source_scan:
                        initialized.add(name)
                        self._state["initialized"] = sorted(initialized)
                        self._save()

                    for author, fid in moods:
                        key = author + ":" + fid
                        if key in self._state["resolved"]:
                            continue
                        if any(
                            p["author"] == author and p["fid"] == fid
                            for p in self._state["pending"]
                        ):
                            continue
                        seen = key in self._state["seen"]
                        published = published_times.get((author, fid))
                        recent = (
                            published is not None
                            and started <= published <= time.time() + 60
                            and time.time() - published <= 86400
                        )
                        if seen:
                            # Legacy baseline: previous versions indexed these
                            # posts without marking whether a like was attempted.
                            # Read-only verification occurs when dequeued.
                            if not recent:
                                continue
                            if self._enqueue(author, fid, published, legacy=True):
                                result["queued"] += 1
                            continue

                        # Pre-activation history is never queued.
                        # If the first scan lacks a trustworthy publish time,
                        # conservatively only baseline that source's posts.
                        if (
                            (first_source_scan and not recent)
                            or (published is not None and not recent)
                        ):
                            self._state["seen"][key] = time.time()
                            result["baseline"] += 1
                            continue
                        if self._enqueue(author, fid, published):
                            result["new"] += 1
                            result["queued"] += 1
                    self._save()

                # Phase 2: process one bounded queue batch. Remaining tasks
                # survive restart and do not need to reappear in the feed.
                await self._drain_pending(gtk, headers, result)
                self._last_error = ""
            except QzoneAccessError as exc:
                self._suspended_until = time.time() + 3600
                self._last_error = str(exc)
                self._log("warning", self._last_error)
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:100]}"
                self._log("warning", self._last_error)
            finally:
                self._last_scan = time.time()
                self._baseline_count += result["baseline"]
                result["pending"] = len(self._state["pending"])
                self._save()
            return result

    async def _worker(self) -> None:
        try:
            while self.enabled:
                summary = await self.scan_once()
                self._log(
                    "info",
                    "本轮结束：新发现=%d 入队=%d 尝试=%d 点赞成功=%d 初次索引=%d"
                    " 补漏=%d 已点赞=%d 待处理=%d 失败=%d 已知好友=%d"
                    % (summary["new"], summary["queued"], summary["attempted"],
                       summary["liked"], summary["baseline"], summary["recovered"],
                       summary["already_liked"], summary["pending"], summary["failed"],
                       len(self._friends)),
                )
                await self._sleep(self.interval)
        except asyncio.CancelledError:
            pass
