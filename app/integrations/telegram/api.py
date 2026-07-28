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

    def __post_init__(self) -> None:
        self._base_url = f"https://api.telegram.org/bot{self.token}"

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
                "allowed_updates": ["message"],
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
    ) -> None:
        chunks = _split_message(text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if index == 0 and reply_to_message_id is not None:
                payload["reply_parameters"] = {
                    "message_id": reply_to_message_id,
                    "allow_sending_without_reply": True,
                }
            await self.call("sendMessage", payload, timeout=30.0)

    async def send_typing(self, chat_id: int) -> None:
        await self.call(
            "sendChatAction",
            {"chat_id": chat_id, "action": "typing"},
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


__all__ = ["TelegramAPIError", "TelegramBotAPI"]
