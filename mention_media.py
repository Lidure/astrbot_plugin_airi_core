"""Collect current and quoted image attachments for @Bot LLM requests.

AstrBot's regular agent build converts Image/Reply segments into req.image_urls
when it constructs the ProviderRequest. An explicit plugin ProviderRequest
skips that branch, so AiriCore must pass real image inputs itself.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# Keep memory and prompt load bounded on low-resource QQ bot hosts.
MAX_MENTION_IMAGES = 12


async def collect_mention_image_urls(
    event: Any,
    *,
    image_type: type,
    reply_type: type,
    max_images: int = MAX_MENTION_IMAGES,
    fallback_extractor: Callable[[Any, Any], Awaitable[list[str]]] | None = None,
    logger: Any = None,
) -> list[str]:
    """Extract images from the active message and its Reply chains.

    Uses AstrBot's own image media conversion, then falls back to the official
    quoted-message image extractor for Reply IDs/placeholder-only chains.
    Resolve only the current user's message/quote; never scan conversation
    history or issue additional LLM requests.
    """
    max_images = max(1, min(int(max_images), MAX_MENTION_IMAGES))
    seen: set[str] = set()
    result: list[str] = []

    def append(ref: str | None) -> None:
        if isinstance(ref, str):
            ref = ref.strip()
            if ref and ref not in seen and len(result) < max_images:
                result.append(ref)
                seen.add(ref)

    async def convert(image: Any) -> bool:
        try:
            # The regular AstrBot path also uses this MediaResolver-backed
            # method; preprocessed QQ/OneBot images will already be local.
            path = await image.convert_to_file_path()
            if path:
                append(str(path))
                return True
        except Exception as exc:
            if logger:
                logger.warning(
                    "Airi @唤醒图片转换失败: %s", type(exc).__name__
                )
        return False

    for component in event.get_messages():
        if len(result) >= max_images:
            break
        if isinstance(component, image_type):
            await convert(component)
        elif isinstance(component, reply_type):
            found_embedded = False
            for quoted in (getattr(component, "chain", None) or []):
                if len(result) >= max_images:
                    break
                if isinstance(quoted, image_type) and await convert(quoted):
                    found_embedded = True
            if not found_embedded and len(result) < max_images:
                # A Reply may contain only an ID or textual [图片] placeholder.
                # AstrBot's extractor can retrieve get_msg/forward records and
                # resolve OneBot image identifiers to actual image references.
                try:
                    extractor = fallback_extractor
                    if extractor is None:
                        from astrbot.core.utils.quoted_message import (
                            extract_quoted_message_images,
                        )
                        extractor = extract_quoted_message_images
                    refs = await extractor(event, component)
                    for ref in refs or []:
                        if len(result) >= max_images:
                            break
                        append(ref)
                except Exception as exc:
                    if logger:
                        logger.warning(
                            "Airi @唤醒引用图片提取失败: %s",
                            type(exc).__name__,
                        )
    return result


def quoted_image_placeholder_present(
    event: Any, *, reply_type: type, image_type: type
) -> bool:
    """Was a quoted image indicated even though resolution might have failed?"""
    for comp in event.get_messages():
        if not isinstance(comp, reply_type):
            continue
        if any(isinstance(item, image_type) for item in (getattr(comp, "chain", None) or [])):
            return True
        text = str(getattr(comp, "message_str", "") or "")
        if "[图片]" in text or "[Image]" in text:
            return True
        if any(
            "[图片]" in str(getattr(item, "text", "") or "")
            or "[Image]" in str(getattr(item, "text", "") or "")
            for item in (getattr(comp, "chain", None) or [])
        ):
            return True
    return False


def quoted_self_message_context(
    event: Any, *, reply_type: type, image_type: type, self_id: str
) -> str:
    """Ground quoted message ownership in QQ/OneBot sender ID, never nickname.

    AstrBot resolves a Reply.sender_id from OneBot get_msg. If lookup failed,
    its default is 0; treat it as unknown rather than claiming bot ownership.
    """
    bot_id = str(self_id or "").strip()
    if not bot_id or bot_id == "0":
        return ""

    for component in event.get_messages():
        if not isinstance(component, reply_type):
            continue
        sender_id = str(getattr(component, "sender_id", "") or "").strip()
        if not sender_id or sender_id == "0" or sender_id != bot_id:
            continue
        chain = getattr(component, "chain", None) or []
        quoted_text = str(getattr(component, "message_str", "") or "")
        has_image = any(isinstance(item, image_type) for item in chain)
        if not has_image:
            has_image = "[图片]" in quoted_text or "[Image]" in quoted_text
        if not has_image:
            has_image = any(
                "[图片]" in str(getattr(item, "text", "") or "")
                or "[Image]" in str(getattr(item, "text", "") or "")
                for item in chain
            )
        if has_image:
            return (
                "QQ平台确认：用户引用的图片来自你这个机器人QQ账号此前发送的消息。"
                "这可能是命令自动发送的内容；不要声称自己记得具体的命令过程，"
                "也不要把该图误当作用户新上传的图片。"
            )
        return "QQ平台确认：用户引用的是你这个机器人QQ账号此前发送的消息；不要虚构发送缘由。"
    return ""
