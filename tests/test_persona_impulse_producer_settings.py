"""Defect 5 (owner mandate 2026-07-31): the impulse cooldown/daily cap used
to be hardcoded in ``app.workers.persona_impulse_producer`` — invisible to
the owner's standing "everything tunable from the site" requirement. They
now live in ``app.thinking.settings`` (``impulse_cooldown_minutes``,
``impulse_daily_cap``) and must actually be read by the producer, not just
stored and ignored.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from app.thinking.settings import DEFAULTS
from app.workers import persona_impulse_producer as producer_module


class _FakeTelegramConfig:
    bot_token = "fake-token"
    allowed_chat_ids: frozenset[int] = frozenset()

    @classmethod
    def load(cls) -> "_FakeTelegramConfig":
        return cls()


async def _fake_owner_user_id() -> int:
    return 7


class _CapturingProducer:
    """Stand-in for ``PersonaImpulseProducer`` that just records its policy."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.policy = kwargs.get("policy")


captured: dict[str, object] = {}


async def _fake_run_persona_impulse_producer(producer: object, *args: object, **kwargs: object) -> None:
    captured["policy"] = producer.policy


@pytest.fixture(autouse=True)
def _patch_worker_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    captured.clear()
    monkeypatch.setattr(producer_module, "TelegramConfig", _FakeTelegramConfig)
    monkeypatch.setattr(producer_module, "get_owner_user_id", _fake_owner_user_id)
    monkeypatch.setattr(producer_module, "SqliteAutowakeRepository", lambda: object())
    monkeypatch.setattr(producer_module, "AutowakeService", lambda *a, **k: object())
    monkeypatch.setattr(producer_module, "TelegramImpulseContextAdapter", lambda *a, **k: object())
    monkeypatch.setattr(producer_module, "TelegramRepository", lambda: object())
    monkeypatch.setattr(producer_module, "LLMImpulseDecisionAdapter", lambda: object())
    monkeypatch.setattr(producer_module, "PersonaImpulseProducer", _CapturingProducer)
    monkeypatch.setattr(
        producer_module, "run_persona_impulse_producer", _fake_run_persona_impulse_producer
    )


async def test_worker_uses_default_impulse_settings_when_nothing_stored(db) -> None:
    await producer_module.run_persona_impulse_worker()
    policy = captured["policy"]
    assert policy.config.cooldown == timedelta(minutes=DEFAULTS.impulse_cooldown_minutes)
    assert policy.config.daily_cap == DEFAULTS.impulse_daily_cap


async def test_worker_reads_owner_configured_impulse_settings(db) -> None:
    from app.thinking.settings import save_thinking_settings

    await save_thinking_settings(
        dataclasses.replace(DEFAULTS, impulse_cooldown_minutes=45, impulse_daily_cap=6)
    )
    await producer_module.run_persona_impulse_worker()
    policy = captured["policy"]
    assert policy.config.cooldown == timedelta(minutes=45)
    assert policy.config.daily_cap == 6
