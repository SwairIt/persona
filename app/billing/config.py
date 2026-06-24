"""Секреты и роль биллинга.

Креды ЮKassa НЕ хранятся в БД и НЕ в git: сначала из переменных окружения,
иначе из файла {PERSONA_DATA_DIR}/billing_secrets.json (рядом с БД, вне репо).
Это сознательно — недавний аудит ловил plaintext-секреты в БД/HTML.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class YooKassaCredentials:
    shop_id: str
    secret_key: str
    live: bool


def data_dir() -> Path:
    d = os.environ.get("PERSONA_DATA_DIR")
    return Path(d) if d else Path.home() / ".persona"


def _secrets_file() -> Path:
    return data_dir() / "billing_secrets.json"


def get_credentials() -> YooKassaCredentials | None:
    """shopId+secret_key ЮKassa: env > файл в data_dir. None — если не настроено."""
    shop = (os.environ.get("PERSONA_YOOKASSA_SHOP_ID") or "").strip()
    secret = (os.environ.get("PERSONA_YOOKASSA_SECRET_KEY") or "").strip()
    if shop and secret:
        live = (os.environ.get("PERSONA_YOOKASSA_LIVE") or "1").strip() == "1"
        return YooKassaCredentials(shop, secret, live)
    f = _secrets_file()
    if f.exists():
        try:
            data = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        shop = (str(data.get("shop_id") or "")).strip()
        secret = (str(data.get("secret_key") or "")).strip()
        if shop and secret:
            return YooKassaCredentials(shop, secret, bool(data.get("live", True)))
    return None


def is_configured() -> bool:
    return get_credentials() is not None


def save_credentials(shop_id: str, secret_key: str, live: bool = True) -> None:
    """Owner-only: записать креды в data-dir файл (не в git, не в БД)."""
    f = _secrets_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps({"shop_id": shop_id.strip(), "secret_key": secret_key.strip(), "live": live}),
        "utf-8",
    )
    try:
        os.chmod(f, 0o600)  # best-effort (на Windows игнорируется)
    except OSError:
        pass
