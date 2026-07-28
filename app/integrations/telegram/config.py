"""Configuration for the Telegram adapter.

Values come from the real environment first and ``.env`` second.  The bot
token is intentionally excluded from repr so an exception/debug dump cannot
leak it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


def _value(dotenv: dict[str, str | None], name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None:
        raw = dotenv.get(name)
    return str(raw or default).strip()


def _integer(raw: str) -> int | None:
    try:
        return int(raw.strip()) if raw.strip() else None
    except ValueError:
        return None


def _id_set(raw: str) -> frozenset[int]:
    values: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        value = _integer(part)
        if value is not None:
            values.add(value)
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Validated runtime configuration.

    ``owner_telegram_user_id`` is optional because a new installation can be
    securely paired with a one-time code.  Until one of those two mechanisms
    binds an owner, every ordinary Telegram update is denied.
    """

    bot_token: str = field(repr=False)
    owner_telegram_user_id: int | None = None
    allowed_chat_ids: frozenset[int] = frozenset()
    pairing_secret: str = field(default="", repr=False)
    poll_timeout_seconds: int = 25

    @classmethod
    def load(cls, env_path: Path | str = ".env") -> TelegramConfig:
        values = dotenv_values(Path(env_path))
        timeout = _integer(_value(values, "PERSONA_TG_POLL_TIMEOUT", "25")) or 25
        return cls(
            bot_token=_value(values, "PERSONA_TG_BOT_TOKEN"),
            owner_telegram_user_id=_integer(_value(values, "PERSONA_TG_OWNER_USER_ID")),
            allowed_chat_ids=_id_set(_value(values, "PERSONA_TG_ALLOWED_CHAT_IDS")),
            pairing_secret=_value(values, "PERSONA_TG_PAIRING_SECRET"),
            poll_timeout_seconds=max(1, min(timeout, 50)),
        )

    def require_token(self) -> None:
        if not self.bot_token:
            raise RuntimeError(
                "PERSONA_TG_BOT_TOKEN is missing. Add it to .env on the "
                "machine that runs the Telegram worker."
            )
