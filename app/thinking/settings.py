"""Owner-controlled settings for Persona's self-directed thinking loop.

Everything the owner can tune — on/off, how deep chains go, the daily
budget, and which seed kinds are allowed — lives in ``kv_settings`` behind
a single ``ThinkingSettings`` dataclass, mirroring the read-with-fallback
pattern used by :mod:`app.web.routes.theme`.

Loading is TOTAL: :func:`load_thinking_settings` is called on every
iteration of a worker loop, so it must never raise. Any unparsable,
missing or out-of-range stored value silently falls back to that
field's default. Saving is the opposite: :func:`save_thinking_settings`
is reached from a web form, so it validates strictly and raises
``ValueError`` when the owner typed something invalid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

ALL_SEED_KINDS: Final[tuple[str, ...]] = (
    "know_you",
    "unfinished",
    "self_check",
    "alive",
)

_CAP_MODES: Final[frozenset[str]] = frozenset({"fixed", "model"})

_MODEL_MAX_LEN: Final[int] = 200


@dataclass(frozen=True, slots=True)
class ThinkingSettings:
    enabled: bool
    cap_mode: str
    step_cap: int
    emergency_cap: int
    daily_budget: int
    seed_kinds: tuple[str, ...]
    may_write_to_chat: bool
    model: str = ""


DEFAULTS: Final[ThinkingSettings] = ThinkingSettings(
    enabled=False,
    cap_mode="fixed",
    step_cap=5,
    emergency_cap=50,
    daily_budget=60,
    seed_kinds=ALL_SEED_KINDS,
    may_write_to_chat=False,
    model="",
)

_KEY_ENABLED = "thinking_enabled"
_KEY_CAP_MODE = "thinking_cap_mode"
_KEY_STEP_CAP = "thinking_step_cap"
_KEY_EMERGENCY_CAP = "thinking_emergency_cap"
_KEY_DAILY_BUDGET = "thinking_daily_budget"
_KEY_SEED_KINDS = "thinking_seed_kinds"
_KEY_MAY_WRITE_TO_CHAT = "thinking_may_write_to_chat"
_KEY_MODEL = "thinking_model"


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    return default


def _parse_positive_int(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return default
    if value <= 0:
        return default
    return value


def _parse_cap_mode(raw: str | None, default: str) -> str:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value not in _CAP_MODES:
        return default
    return value


def _parse_seed_kinds(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    known = tuple(
        kind
        for kind in (part.strip() for part in raw.split(","))
        if kind in ALL_SEED_KINDS
    )
    if not known:
        return ALL_SEED_KINDS
    return known


def _parse_model(raw: str | None, default: str) -> str:
    if raw is None:
        return default
    return raw.strip()[:_MODEL_MAX_LEN]


async def load_thinking_settings() -> ThinkingSettings:
    """Load thinking settings from ``kv_settings``, never raising.

    Any unparsable, missing or out-of-range value falls back to that
    field's default rather than propagating — this is read on every
    iteration of a worker loop and a crash here must never silently
    kill the whole feature.
    """
    try:
        async with get_connection() as conn:
            raw_enabled = await get_kv(conn, _KEY_ENABLED)
            raw_cap_mode = await get_kv(conn, _KEY_CAP_MODE)
            raw_step_cap = await get_kv(conn, _KEY_STEP_CAP)
            raw_emergency_cap = await get_kv(conn, _KEY_EMERGENCY_CAP)
            raw_daily_budget = await get_kv(conn, _KEY_DAILY_BUDGET)
            raw_seed_kinds = await get_kv(conn, _KEY_SEED_KINDS)
            raw_may_write_to_chat = await get_kv(conn, _KEY_MAY_WRITE_TO_CHAT)
            raw_model = await get_kv(conn, _KEY_MODEL)
    except Exception:
        return DEFAULTS

    return ThinkingSettings(
        enabled=_parse_bool(raw_enabled, DEFAULTS.enabled),
        cap_mode=_parse_cap_mode(raw_cap_mode, DEFAULTS.cap_mode),
        step_cap=_parse_positive_int(raw_step_cap, DEFAULTS.step_cap),
        emergency_cap=_parse_positive_int(raw_emergency_cap, DEFAULTS.emergency_cap),
        daily_budget=_parse_positive_int(raw_daily_budget, DEFAULTS.daily_budget),
        seed_kinds=_parse_seed_kinds(raw_seed_kinds, DEFAULTS.seed_kinds),
        may_write_to_chat=_parse_bool(raw_may_write_to_chat, DEFAULTS.may_write_to_chat),
        model=_parse_model(raw_model, DEFAULTS.model),
    )


def _validate(settings: ThinkingSettings) -> None:
    if settings.cap_mode not in _CAP_MODES:
        raise ValueError(f"invalid cap_mode: {settings.cap_mode!r}")
    if settings.step_cap <= 0:
        raise ValueError("step_cap must be positive")
    if settings.emergency_cap <= 0:
        raise ValueError("emergency_cap must be positive")
    if settings.daily_budget <= 0:
        raise ValueError("daily_budget must be positive")
    if not settings.seed_kinds:
        raise ValueError("seed_kinds must not be empty")
    unknown = [kind for kind in settings.seed_kinds if kind not in ALL_SEED_KINDS]
    if unknown:
        raise ValueError(f"unknown seed kinds: {unknown!r}")
    if any(ch.isspace() or not ch.isprintable() for ch in settings.model):
        raise ValueError(f"invalid model name: {settings.model!r}")


async def save_thinking_settings(settings: ThinkingSettings) -> None:
    """Validate strictly and persist to ``kv_settings``.

    Reached from a web form, so unlike loading this raises ``ValueError``
    on anything invalid — the owner needs to be told they typed
    something wrong, not have it silently swapped for a default.
    """
    _validate(settings)

    async with get_connection() as conn:
        await set_kv(conn, _KEY_ENABLED, "true" if settings.enabled else "false")
        await set_kv(conn, _KEY_CAP_MODE, settings.cap_mode)
        await set_kv(conn, _KEY_STEP_CAP, str(settings.step_cap))
        await set_kv(conn, _KEY_EMERGENCY_CAP, str(settings.emergency_cap))
        await set_kv(conn, _KEY_DAILY_BUDGET, str(settings.daily_budget))
        await set_kv(conn, _KEY_SEED_KINDS, ",".join(settings.seed_kinds))
        await set_kv(
            conn, _KEY_MAY_WRITE_TO_CHAT, "true" if settings.may_write_to_chat else "false"
        )
        await set_kv(conn, _KEY_MODEL, settings.model)


def effective_cap(settings: ThinkingSettings) -> int:
    """Return the step count after which a conclusion is forced.

    ``step_cap`` in ``"fixed"`` mode; ``emergency_cap`` in ``"model"``
    mode, where the model itself decides when to conclude and the
    emergency cap exists only to stop a chain that never terminates.
    """
    if settings.cap_mode == "model":
        return settings.emergency_cap
    return settings.step_cap


__all__ = [
    "ALL_SEED_KINDS",
    "DEFAULTS",
    "ThinkingSettings",
    "effective_cap",
    "load_thinking_settings",
    "save_thinking_settings",
]
