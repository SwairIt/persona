"""Бейдж приватности в чате: локальный провайдер → 🔒, облачный → ☁ (S2b-cont)."""

from __future__ import annotations

import aiosqlite
import pytest

from app.storage.repository import set_kv
from app.web.routes.chat_sessions import _provider_badge


@pytest.mark.asyncio
async def test_badge_local_default(db: aiosqlite.Connection) -> None:
    # Без явного kv провайдер = ollama (локально) → 🔒.
    badge = await _provider_badge()
    assert badge["provider"] == "ollama"
    assert badge["is_local"] is True


@pytest.mark.asyncio
async def test_badge_cloud(db: aiosqlite.Connection) -> None:
    await set_kv(db, "llm_provider", "OpenAI")  # регистр/пробелы нормализуются
    await db.commit()
    badge = await _provider_badge()
    assert badge["provider"] == "openai"
    assert badge["is_local"] is False
