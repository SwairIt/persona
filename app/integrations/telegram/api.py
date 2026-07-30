"""Small Telegram Bot API transport with secret-safe errors.

The project does not need a full bot framework for long polling and plain
text replies.  Keeping this adapter on the standard library also avoids an
additional always-on dependency.  Network calls run in ``asyncio.to_thread``
so they never block Persona's event loop.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from urllib import error, request


class TelegramAPIError(RuntimeError):
    """An upstream failure that never contains the token-bearing URL."""


@dataclass(slots=True)
class TelegramBotAPI:
    token: str = field(repr=False)
    _base_url: str = field(init=False, repr=False)
    _file_base_url: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._base_url = f"https://api.telegram.org/bot{self.token}"
        self._file_base_url = f"https://api.telegram.org/file/bot{self.token}"

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 40.0,
    ) -> Any:
        return await asyncio.to_thread(self._call_sync, method, payload or {}, timeout)

    def _call_sync(self, method: str, payload: dict[str, Any], timeout: float) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(  # noqa: S310 - fixed HTTPS Telegram API origin
            f"{self._base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as response:  # noqa: S310
                raw = response.read()
        except error.HTTPError as exc:
            description = _telegram_description(exc.read())
            raise TelegramAPIError(
                f"Telegram API {method} returned HTTP {exc.code}: {description}"
            ) from None
        except (error.URLError, TimeoutError, OSError) as exc:
            # Do not include ``exc``: urllib errors may contain the complete
            # request URL, and Telegram puts the bot token in that URL.
            raise TelegramAPIError(
                f"Telegram API {method} is temporarily unavailable ({type(exc).__name__})"
            ) from None
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramAPIError(f"Telegram API {method} returned invalid JSON") from None
        if not isinstance(decoded, dict) or not decoded.get("ok"):
            description = str(
                decoded.get("description", "unknown error")
                if isinstance(decoded, dict)
                else "unknown error"
            )
            raise TelegramAPIError(f"Telegram API {method}: {description[:300]}")
        return decoded.get("result")

    async def get_me(self) -> dict[str, Any]:
        result = await self.call("getMe")
        return result if isinstance(result, dict) else {}

    async def get_updates(self, offset: int, timeout_seconds: int) -> list[dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout_seconds,
                # "callback_query" delivers the confirm/cancel button presses
                # for parked execution-class actions (see pending_actions.py).
                "allowed_updates": ["message", "edited_message", "callback_query"],
            },
            timeout=float(timeout_seconds + 15),
        )
        return [item for item in (result or []) if isinstance(item, dict)]

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> tuple[int, ...]:
        chunks = _split_message(text)
        sent_ids: list[int] = []
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "link_preview_options": {"is_disabled": True},
            }
            if index == 0 and reply_to_message_id is not None:
                payload["reply_parameters"] = {
                    "message_id": reply_to_message_id,
                    "allow_sending_without_reply": True,
                }
            result = await self.call("sendMessage", payload, timeout=30.0)
            message_id = _message_id(result)
            if message_id is not None:
                sent_ids.append(message_id)
        return tuple(sent_ids)

    async def send_message_with_buttons(
        self,
        chat_id: int,
        text: str,
        buttons: list[tuple[str, str]],
        *,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        """Send one message with an inline keyboard, one button per row.

        ``buttons`` are ``(label, callback_data)`` pairs. Used for the
        execution-action confirmation card: ``callback_data`` carries ONLY
        the opaque pending id -- never the tool name or arguments, which
        the callback handler must always re-read from the parked DB row
        (see ``PendingActionStore``), not from this payload.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4_000],
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data}]
                    for label, data in buttons
                ]
            },
        }
        _set_reply(payload, reply_to_message_id)
        return _message_id(await self.call("sendMessage", payload, timeout=30.0))

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        await self.call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text[:200],
                "show_alert": show_alert,
            },
            timeout=10.0,
        )

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        await self.call(
            "sendChatAction",
            {"chat_id": chat_id, "action": action},
            timeout=10.0,
        )

    async def send_typing(self, chat_id: int) -> None:
        await self.send_chat_action(chat_id, "typing")

    async def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        emoji: str | None,
    ) -> None:
        reaction = (
            [{"type": "emoji", "emoji": emoji}]
            if emoji
            else []
        )
        await self.call(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": reaction,
            },
            timeout=10.0,
        )

    async def send_media(
        self,
        kind: str,
        chat_id: int,
        media: str,
        *,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        methods = {
            "photo": ("sendPhoto", "photo", "upload_photo"),
            "document": ("sendDocument", "document", "upload_document"),
            "audio": ("sendAudio", "audio", "upload_document"),
            "video": ("sendVideo", "video", "upload_video"),
            "animation": ("sendAnimation", "animation", "upload_video"),
            "voice": ("sendVoice", "voice", "record_voice"),
            "sticker": ("sendSticker", "sticker", "choose_sticker"),
        }
        selected = methods.get(kind)
        if selected is None:
            raise ValueError("unsupported Telegram media kind")
        method, field_name, action = selected
        await self.send_chat_action(chat_id, action)
        payload: dict[str, Any] = {"chat_id": chat_id, field_name: media}
        if caption and kind != "sticker":
            payload["caption"] = caption[:1_000]
        _set_reply(payload, reply_to_message_id)
        return _message_id(await self.call(method, payload, timeout=45.0))

    async def send_dice(
        self,
        chat_id: int,
        *,
        reply_to_message_id: int | None = None,
        emoji: str = "🎲",
    ) -> int | None:
        payload: dict[str, Any] = {"chat_id": chat_id, "emoji": emoji}
        _set_reply(payload, reply_to_message_id)
        return _message_id(await self.call("sendDice", payload, timeout=15.0))

    async def send_poll(
        self,
        chat_id: int,
        question: str,
        options: tuple[str, ...],
        *,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "question": question[:300],
            "options": [{"text": option[:100]} for option in options[:10]],
        }
        _set_reply(payload, reply_to_message_id)
        return _message_id(await self.call("sendPoll", payload, timeout=20.0))

    async def send_location(
        self,
        chat_id: int,
        latitude: float,
        longitude: float,
        *,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "latitude": latitude,
            "longitude": longitude,
        }
        _set_reply(payload, reply_to_message_id)
        return _message_id(await self.call("sendLocation", payload, timeout=15.0))

    async def send_contact(
        self,
        chat_id: int,
        phone_number: str,
        first_name: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "phone_number": phone_number[:40],
            "first_name": first_name[:120],
        }
        _set_reply(payload, reply_to_message_id)
        return _message_id(await self.call("sendContact", payload, timeout=15.0))

    async def copy_message(
        self,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
        *,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        }
        _set_reply(payload, reply_to_message_id)
        return _message_id(await self.call("copyMessage", payload, timeout=20.0))

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        await self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:4_000],
                "link_preview_options": {"is_disabled": True},
            },
            timeout=20.0,
        )

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self.call(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
            timeout=15.0,
        )

    async def get_file(self, file_id: str) -> dict[str, Any]:
        result = await self.call(
            "getFile",
            {"file_id": file_id},
            timeout=20.0,
        )
        return result if isinstance(result, dict) else {}

    async def download_file(self, file_path: str, *, max_bytes: int) -> bytes:
        return await asyncio.to_thread(
            self._download_file_sync,
            file_path,
            max(1, max_bytes),
        )

    def _download_file_sync(self, file_path: str, max_bytes: int) -> bytes:
        clean = str(file_path or "").strip().replace("\\", "/")
        if (
            not clean
            or clean.startswith("/")
            or ".." in clean.split("/")
            or "://" in clean
            or len(clean) > 500
        ):
            raise TelegramAPIError("Telegram returned an invalid file path")
        req = request.Request(  # noqa: S310 - fixed Telegram file origin
            f"{self._file_base_url}/{clean}",
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=30.0) as response:  # noqa: S310
                data = response.read(max_bytes + 1)
        except (error.HTTPError, error.URLError, TimeoutError, OSError) as exc:
            raise TelegramAPIError(
                "Telegram file download is temporarily unavailable "
                f"({type(exc).__name__})"
            ) from None
        if len(data) > max_bytes:
            raise TelegramAPIError("Telegram file exceeds the configured size limit")
        return bytes(data)

    async def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        await self.call(
            "setMyCommands",
            {"commands": commands},
            timeout=10.0,
        )


def _telegram_description(raw: bytes) -> str:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "upstream error"
    if isinstance(payload, dict):
        return str(payload.get("description") or "upstream error")[:300]
    return "upstream error"


def _split_message(text: str, limit: int = 3900) -> list[str]:
    """Split without Telegram markdown parsing and preserve readable breaks."""
    remaining = (text or "").strip() or "(пустой ответ)"
    chunks: list[str] = []
    while len(remaining) > limit:
        boundary = max(
            remaining.rfind("\n", 0, limit),
            remaining.rfind(" ", 0, limit),
        )
        if boundary < limit // 2:
            boundary = limit
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _message_id(result: Any) -> int | None:
    if not isinstance(result, dict):
        return None
    value = result.get("message_id")
    if not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _set_reply(payload: dict[str, Any], message_id: int | None) -> None:
    if message_id is not None:
        payload["reply_parameters"] = {
            "message_id": message_id,
            "allow_sending_without_reply": True,
        }


__all__ = ["TelegramAPIError", "TelegramBotAPI"]
