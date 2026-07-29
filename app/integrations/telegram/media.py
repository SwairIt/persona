"""Safe extraction and bounded inspection of Telegram attachments."""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.integrations.telegram.api import TelegramBotAPI

_MAX_VISION_BYTES = 10 * 1024 * 1024
_MAX_TEXT_BYTES = 256 * 1024
_MAX_AUDIO_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TelegramAttachment:
    kind: str
    file_id: str
    file_unique_id: str = ""
    file_name: str = ""
    mime_type: str = ""
    file_size: int | None = None

    def summary(self) -> str:
        details = [self.kind]
        if self.file_name:
            details.append(self.file_name[:180])
        if self.mime_type:
            details.append(self.mime_type[:120])
        if self.file_size is not None:
            details.append(f"{self.file_size} bytes")
        return ", ".join(details)


@dataclass(frozen=True, slots=True)
class TelegramMediaContext:
    text_suffix: str = ""
    image_data_url: str | None = None


def attachments_from_message(message: dict[str, Any]) -> tuple[TelegramAttachment, ...]:
    items: list[TelegramAttachment] = []
    photos = message.get("photo")
    if isinstance(photos, list):
        candidates = [item for item in photos if isinstance(item, dict)]
        if candidates:
            chosen = max(
                candidates,
                key=lambda item: int(item.get("file_size") or 0),
            )
            attachment = _attachment("photo", chosen)
            if attachment is not None:
                items.append(attachment)
    for kind in (
        "document",
        "audio",
        "video",
        "animation",
        "voice",
        "video_note",
        "sticker",
    ):
        raw = message.get(kind)
        if isinstance(raw, dict):
            attachment = _attachment(kind, raw)
            if attachment is not None:
                items.append(attachment)
    return tuple(items[:8])


def non_file_content_summary(message: dict[str, Any]) -> str:
    location = message.get("location")
    if isinstance(location, dict):
        return (
            "[Геолокация Telegram: "
            f"{location.get('latitude')}, {location.get('longitude')}]"
        )
    contact = message.get("contact")
    if isinstance(contact, dict):
        return (
            "[Контакт Telegram: "
            f"{contact.get('first_name') or ''} "
            f"{contact.get('last_name') or ''}, "
            f"{contact.get('phone_number') or ''}]"
        ).strip()
    poll = message.get("poll")
    if isinstance(poll, dict):
        options = poll.get("options")
        labels = [
            str(item.get("text") or "")
            for item in options or []
            if isinstance(item, dict)
        ]
        return (
            f"[Опрос Telegram: {poll.get('question') or ''}; "
            f"варианты: {', '.join(labels[:10])}]"
        )
    dice = message.get("dice")
    if isinstance(dice, dict):
        return f"[Кубик Telegram: {dice.get('emoji') or '🎲'} = {dice.get('value')}]"
    return ""


async def build_media_context(
    api: TelegramBotAPI,
    attachments: tuple[TelegramAttachment, ...],
) -> TelegramMediaContext:
    summaries = [f"[Вложение Telegram: {item.summary()}]" for item in attachments]
    image_data_url: str | None = None
    extra_text = ""
    for item in attachments:
        if image_data_url is None and _is_image(item):
            raw = await _download(api, item, _MAX_VISION_BYTES)
            if raw:
                mime = item.mime_type or "image/jpeg"
                encoded = base64.b64encode(raw).decode("ascii")
                image_data_url = f"data:{mime};base64,{encoded}"
        if item.kind == "document" and item.mime_type.startswith("text/"):
            raw = await _download(api, item, _MAX_TEXT_BYTES)
            if raw:
                decoded = raw.decode("utf-8", errors="replace").strip()
                if decoded:
                    extra_text += f"\n[Содержимое файла]\n{decoded[:12_000]}"
        if item.kind in {"voice", "audio"}:
            transcript = await _transcribe(api, item)
            if transcript:
                extra_text += f"\n[Расшифровка аудио]\n{transcript[:8_000]}"
    suffix = "\n".join(summaries) + extra_text
    return TelegramMediaContext(text_suffix=suffix.strip(), image_data_url=image_data_url)


async def _download(
    api: TelegramBotAPI,
    attachment: TelegramAttachment,
    limit: int,
) -> bytes | None:
    if attachment.file_size is not None and attachment.file_size > limit:
        return None
    try:
        file_info = await api.get_file(attachment.file_id)
        path = str(file_info.get("file_path") or "")
        return await api.download_file(path, max_bytes=limit)
    except Exception:
        return None


async def _transcribe(
    api: TelegramBotAPI,
    attachment: TelegramAttachment,
) -> str | None:
    raw = await _download(api, attachment, _MAX_AUDIO_BYTES)
    if not raw:
        return None
    suffix = {
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
    }.get(attachment.mime_type, ".ogg" if attachment.kind == "voice" else ".bin")
    descriptor, raw_path = tempfile.mkstemp(prefix="persona-tg-", suffix=suffix)
    os.close(descriptor)
    path = Path(raw_path)
    try:
        await asyncio.to_thread(path.write_bytes, raw)
        from app.audio.transcribe import transcribe_segment  # noqa: PLC0415

        async with asyncio.timeout(120.0):
            return await transcribe_segment(path)
    except Exception:
        return None
    finally:
        with suppress(OSError):
            path.unlink(missing_ok=True)


def _attachment(kind: str, raw: dict[str, Any]) -> TelegramAttachment | None:
    file_id = str(raw.get("file_id") or "").strip()
    if not file_id:
        return None
    size_raw = raw.get("file_size")
    try:
        size = int(size_raw) if size_raw is not None else None
    except (TypeError, ValueError):
        size = None
    return TelegramAttachment(
        kind=kind,
        file_id=file_id,
        file_unique_id=str(raw.get("file_unique_id") or ""),
        file_name=str(raw.get("file_name") or ""),
        mime_type=str(raw.get("mime_type") or ""),
        file_size=size,
    )


def _is_image(item: TelegramAttachment) -> bool:
    return item.kind == "photo" or (
        item.kind == "document"
        and item.mime_type in {"image/jpeg", "image/png", "image/webp"}
    )


__all__ = [
    "TelegramAttachment",
    "TelegramMediaContext",
    "attachments_from_message",
    "build_media_context",
    "non_file_content_summary",
]
