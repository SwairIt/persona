from __future__ import annotations

import dataclasses

import pytest

from app.thinking.settings import (
    ALL_SEED_KINDS,
    DEFAULTS,
    ThinkingSettings,
    effective_cap,
    load_thinking_settings,
    save_thinking_settings,
)


async def test_defaults_are_returned_when_nothing_is_stored(db) -> None:
    loaded = await load_thinking_settings()
    assert loaded == DEFAULTS
    assert loaded.enabled is False, "thinking must be OFF until the owner turns it on"
    assert set(loaded.seed_kinds) == set(ALL_SEED_KINDS)
    assert loaded.quiet_minutes == 3, "thinking's own gate, not the dream cycle's 60"


async def test_settings_round_trip(db) -> None:
    saved = ThinkingSettings(
        enabled=True,
        cap_mode="model",
        step_cap=7,
        emergency_cap=100,
        daily_budget=200,
        seed_kinds=("know_you", "alive"),
        may_write_to_chat=True,
    )
    await save_thinking_settings(saved)
    assert await load_thinking_settings() == saved


async def test_effective_cap_follows_the_mode() -> None:
    fixed = ThinkingSettings(
        enabled=True, cap_mode="fixed", step_cap=5, emergency_cap=50,
        daily_budget=60, seed_kinds=ALL_SEED_KINDS, may_write_to_chat=False,
    )
    assert effective_cap(fixed) == 5
    model = ThinkingSettings(
        enabled=True, cap_mode="model", step_cap=5, emergency_cap=50,
        daily_budget=60, seed_kinds=ALL_SEED_KINDS, may_write_to_chat=False,
    )
    assert effective_cap(model) == 50, (
        "in model-decides mode only the emergency cap forces a conclusion"
    )


async def test_corrupt_stored_values_fall_back_to_defaults(db) -> None:
    """A hand-edited or half-written kv row must not crash the worker."""
    from app.storage.db import get_connection
    from app.storage.repository import set_kv

    async with get_connection() as conn:
        await set_kv(conn, "thinking_step_cap", "не число")
        await set_kv(conn, "thinking_cap_mode", "нечто")
        await set_kv(conn, "thinking_seed_kinds", "know_you,выдумка")
        await conn.commit()

    loaded = await load_thinking_settings()
    assert loaded.step_cap == DEFAULTS.step_cap
    assert loaded.cap_mode == DEFAULTS.cap_mode
    assert loaded.seed_kinds == ("know_you",), "unknown seed kinds are dropped"


@pytest.mark.parametrize(
    "field", ["step_cap", "emergency_cap", "daily_budget"]
)
async def test_non_positive_numbers_are_rejected_on_save(db, field) -> None:
    # dataclasses.replace, not DEFAULTS.__dict__: the dataclass uses slots=True
    # and therefore has no __dict__.
    bad = dataclasses.replace(DEFAULTS, **{field: 0})
    with pytest.raises(ValueError):
        await save_thinking_settings(bad)


async def test_unknown_cap_mode_is_rejected_on_save(db) -> None:
    with pytest.raises(ValueError):
        await save_thinking_settings(dataclasses.replace(DEFAULTS, cap_mode="нечто"))


async def test_model_defaults_to_empty_string() -> None:
    assert DEFAULTS.model == ""


async def test_model_round_trips_through_save_and_load(db) -> None:
    saved = dataclasses.replace(DEFAULTS, model="qwen2.5:7b")
    await save_thinking_settings(saved)
    loaded = await load_thinking_settings()
    assert loaded.model == "qwen2.5:7b"


async def test_model_with_whitespace_is_rejected_on_save(db) -> None:
    with pytest.raises(ValueError):
        await save_thinking_settings(dataclasses.replace(DEFAULTS, model="qwen2.5 7b"))


async def test_quiet_minutes_round_trips_through_save_and_load(db) -> None:
    saved = dataclasses.replace(DEFAULTS, quiet_minutes=10)
    await save_thinking_settings(saved)
    loaded = await load_thinking_settings()
    assert loaded.quiet_minutes == 10


async def test_quiet_minutes_out_of_range_is_rejected_on_save(db) -> None:
    with pytest.raises(ValueError):
        await save_thinking_settings(dataclasses.replace(DEFAULTS, quiet_minutes=0))
    with pytest.raises(ValueError):
        await save_thinking_settings(dataclasses.replace(DEFAULTS, quiet_minutes=999999))


async def test_quiet_minutes_corrupt_stored_value_falls_back_to_default(db) -> None:
    from app.storage.db import get_connection
    from app.storage.repository import set_kv

    async with get_connection() as conn:
        await set_kv(conn, "thinking_quiet_minutes", "не число")
        await conn.commit()

    loaded = await load_thinking_settings()
    assert loaded.quiet_minutes == DEFAULTS.quiet_minutes
