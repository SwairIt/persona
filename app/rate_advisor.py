"""Capture-rate adaptive learning advisor (v1.41).

The advisor observes the recent ``daily_budget_state`` history (one row
per UTC day, written by :mod:`app.budget`) and emits a suggested new
combination of ``capture_interval_seconds`` and
``dedup_hamming_threshold`` based on how the observed byte usage
compares to the configured ``daily_budget_mb`` cap.

Decision rules
--------------
Let ``r = avg_daily_mb / cap_mb`` over the last ``lookback_days``
(default 7) UTC days, summing the ``thumbnails + audio + events``
buckets:

* ``r > 0.85`` — under-budgeted: slow the capture loop down
  (``interval * 1.5``) and tighten dedup (``threshold + 1``). Severity
  ``warn``.
* ``r < 0.40`` — over-budgeted: speed the loop up
  (``interval / 1.2``) and loosen dedup (``threshold - 1``, floored at
  ``4``). Severity ``info``.
* otherwise — current values are within the healthy band; the
  advisor still returns a "no change" suggestion (same numbers) so
  the operator sees an explicit confirmation. Severity ``info``.

The advisor never auto-applies. It writes a row to
``rate_advisor_run`` describing what it observed and what it would
recommend; the operator then clicks Apply in the UI, which routes to
:func:`apply_suggestion`.

This module is intentionally pure-Python + parametrised SQL; it does
not depend on the FastAPI layer and is therefore importable from a
CLI worker too.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

log = get_logger("persona.rate_advisor")

Severity = Literal["info", "warn"]

_DEFAULT_LOOKBACK_DAYS = 7
_DEDUP_FLOOR = 4
_HIGH_RATIO = 0.85
_LOW_RATIO = 0.40
_INTERVAL_MIN = 0.5
_INTERVAL_MAX = 60.0
_DEDUP_MIN = 0
_DEDUP_MAX = 64
_BYTES_PER_MB = 1024.0 * 1024.0


def _clamp_interval(value: float) -> float:
    return max(_INTERVAL_MIN, min(_INTERVAL_MAX, float(value)))


def _clamp_dedup(value: int) -> int:
    return max(_DEDUP_MIN, min(_DEDUP_MAX, int(value)))


async def _read_live_interval(default: float) -> float:
    """Return the live capture interval, preferring kv over Settings."""
    async with get_connection() as conn:
        raw = await get_kv(conn, "capture_interval_seconds")
        if raw is None:
            raw = await get_kv(conn, "capture_interval_seconds_live")
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("rate_advisor.interval_kv_invalid", raw=raw)
        return float(default)


async def _read_live_dedup(default: int) -> int:
    """Return the live dedup threshold, preferring kv over Settings."""
    async with get_connection() as conn:
        raw = await get_kv(conn, "dedup_hamming_threshold")
    if raw is None:
        return int(default)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        log.warning("rate_advisor.dedup_kv_invalid", raw=raw)
        return int(default)


async def _read_recent_daily_mb(lookback_days: int) -> tuple[float, int]:
    """Return (avg_daily_mb, row_count) over the last ``lookback_days`` days.

    Sums the ``thumbnails + audio + events`` buckets per row and
    averages across the rows actually present. Missing days contribute
    nothing — we average across what we have, not across a padded
    window, so a fresh install isn't penalised for empty history.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT
                COALESCE(AVG(thumbnails_bytes + audio_bytes + events_bytes), 0.0)
                    AS avg_bytes,
                COUNT(*) AS n
            FROM (
                SELECT thumbnails_bytes, audio_bytes, events_bytes
                FROM daily_budget_state
                ORDER BY day DESC
                LIMIT ?
            )
            """,
            (int(lookback_days),),
        )
        row = await cursor.fetchone()
    if row is None:
        return 0.0, 0
    avg_bytes = float(row["avg_bytes"] or 0.0)
    n = int(row["n"] or 0)
    return avg_bytes / _BYTES_PER_MB, n


async def compute_advisor_state(lookback_days: int = _DEFAULT_LOOKBACK_DAYS) -> dict[str, Any]:
    """Compute the advisor recommendation.

    Returns a JSON-safe dict with keys ``avg_daily_mb``, ``cap_mb``,
    ``current_interval_seconds``, ``current_dedup_threshold``,
    ``suggested_interval``, ``suggested_dedup``, ``rationale``,
    ``severity``, plus ``samples`` (how many daily rows were averaged)
    and ``ratio`` (avg / cap) for the UI.
    """
    if lookback_days < 1:
        lookback_days = _DEFAULT_LOOKBACK_DAYS

    cfg = get_settings()
    cap_mb = float(cfg.daily_budget_mb)
    current_interval = await _read_live_interval(cfg.capture_interval_seconds)
    current_dedup = await _read_live_dedup(cfg.dedup_hamming_threshold)
    avg_daily_mb, samples = await _read_recent_daily_mb(lookback_days)

    ratio = (avg_daily_mb / cap_mb) if cap_mb > 0.0 else 0.0

    severity: Severity = "info"
    if samples == 0:
        suggested_interval = current_interval
        suggested_dedup = current_dedup
        rationale = (
            "Недостаточно данных: за последние "
            f"{lookback_days} дн. в daily_budget_state нет ни одной записи. "
            "Совет невозможен — продолжайте захват и попробуйте позже."
        )
    elif ratio > _HIGH_RATIO:
        suggested_interval = _clamp_interval(current_interval * 1.5)
        suggested_dedup = _clamp_dedup(current_dedup + 1)
        severity = "warn"
        rationale = (
            f"Средний расход {avg_daily_mb:.2f} МБ/день за {samples} дн. — "
            f"{ratio * 100.0:.0f}% от лимита {cap_mb:.1f} МБ. "
            "Рекомендуется замедлить захват x1.5 и ужесточить дедуп +1, "
            "чтобы укладываться в дневной бюджет."
        )
    elif ratio < _LOW_RATIO:
        suggested_interval = _clamp_interval(current_interval / 1.2)
        suggested_dedup = _clamp_dedup(max(_DEDUP_FLOOR, current_dedup - 1))
        rationale = (
            f"Средний расход {avg_daily_mb:.2f} МБ/день за {samples} дн. — "
            f"всего {ratio * 100.0:.0f}% от лимита {cap_mb:.1f} МБ. "
            "Можно ускорить захват /1.2 и ослабить дедуп -1 "
            f"(минимум {_DEDUP_FLOOR}), чтобы собирать больше контекста."
        )
    else:
        suggested_interval = current_interval
        suggested_dedup = current_dedup
        rationale = (
            f"Средний расход {avg_daily_mb:.2f} МБ/день за {samples} дн. — "
            f"{ratio * 100.0:.0f}% от лимита {cap_mb:.1f} МБ. "
            "Текущие настройки в здоровом диапазоне 40-85%, менять не нужно."
        )

    state: dict[str, Any] = {
        "avg_daily_mb": round(avg_daily_mb, 4),
        "cap_mb": round(cap_mb, 4),
        "ratio": round(ratio, 4),
        "samples": samples,
        "lookback_days": int(lookback_days),
        "current_interval_seconds": round(float(current_interval), 4),
        "current_dedup_threshold": int(current_dedup),
        "suggested_interval": round(float(suggested_interval), 4),
        "suggested_dedup": int(suggested_dedup),
        "rationale": rationale,
        "severity": severity,
    }
    log.info(
        "rate_advisor.computed",
        avg_daily_mb=state["avg_daily_mb"],
        cap_mb=state["cap_mb"],
        ratio=state["ratio"],
        samples=state["samples"],
        suggested_interval=state["suggested_interval"],
        suggested_dedup=state["suggested_dedup"],
        severity=state["severity"],
    )
    return state


async def record_advisor_run(state: dict[str, Any]) -> int:
    """Persist a computed advisor state to ``rate_advisor_run``.

    Returns the freshly-minted row id. The row is the canonical
    record of "what the advisor proposed at time T"; the operator may
    later apply it via :func:`apply_suggestion`, which stamps
    ``applied_at`` in-place.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO rate_advisor_run (
                avg_daily_mb,
                cap_mb,
                current_interval_seconds,
                current_dedup_threshold,
                suggested_interval_seconds,
                suggested_dedup_threshold,
                rationale
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(state["avg_daily_mb"]),
                float(state["cap_mb"]),
                float(state["current_interval_seconds"]),
                int(state["current_dedup_threshold"]),
                float(state["suggested_interval"]),
                int(state["suggested_dedup"]),
                str(state.get("rationale") or ""),
            ),
        )
        await conn.commit()
    row_id = cursor.lastrowid
    if row_id is None:
        msg = "INSERT into rate_advisor_run did not return a row id"
        raise RuntimeError(msg)
    log.info("rate_advisor.recorded", run_id=int(row_id))
    return int(row_id)


async def apply_suggestion(run_id: int) -> dict[str, Any]:
    """Apply the suggestion stored under ``run_id`` to live kv settings.

    Writes ``suggested_interval_seconds`` to the
    ``capture_interval_seconds`` (plus the legacy ``_live`` alias for
    parity with :mod:`app.web.routes.capture_settings`) and
    ``suggested_dedup_threshold`` to ``dedup_hamming_threshold``. Then
    stamps ``rate_advisor_run.applied_at`` so the UI history shows
    which suggestion was acted upon. Idempotent: applying twice rewrites
    the same kv values and refreshes ``applied_at``.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT
                id,
                suggested_interval_seconds,
                suggested_dedup_threshold,
                applied_at
            FROM rate_advisor_run
            WHERE id = ?
            """,
            (int(run_id),),
        )
        row = await cursor.fetchone()
        if row is None:
            msg = f"rate_advisor_run id={run_id} not found"
            raise ValueError(msg)

        suggested_interval = _clamp_interval(float(row["suggested_interval_seconds"]))
        suggested_dedup = _clamp_dedup(int(row["suggested_dedup_threshold"]))
        interval_str = f"{suggested_interval:.2f}"
        dedup_str = str(int(suggested_dedup))

        await set_kv(conn, "capture_interval_seconds", interval_str)
        await set_kv(conn, "capture_interval_seconds_live", interval_str)
        await set_kv(conn, "dedup_hamming_threshold", dedup_str)

        await conn.execute(
            """
            UPDATE rate_advisor_run
            SET applied_at = datetime('now')
            WHERE id = ?
            """,
            (int(run_id),),
        )
        await conn.commit()

    result: dict[str, Any] = {
        "run_id": int(run_id),
        "applied_interval_seconds": float(suggested_interval),
        "applied_dedup_threshold": int(suggested_dedup),
    }
    log.info(
        "rate_advisor.applied",
        run_id=result["run_id"],
        interval=result["applied_interval_seconds"],
        dedup=result["applied_dedup_threshold"],
    )
    return result


async def list_recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` advisor runs, newest first.

    Used by the history JSON endpoint and the page-render fallback when
    no fresh run is available.
    """
    limit = max(1, min(100, int(limit)))
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT
                id,
                run_at,
                avg_daily_mb,
                cap_mb,
                current_interval_seconds,
                current_dedup_threshold,
                suggested_interval_seconds,
                suggested_dedup_threshold,
                rationale,
                applied_at
            FROM rate_advisor_run
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row["id"]),
                "run_at": str(row["run_at"]),
                "avg_daily_mb": float(row["avg_daily_mb"]),
                "cap_mb": float(row["cap_mb"]),
                "current_interval_seconds": float(row["current_interval_seconds"]),
                "current_dedup_threshold": int(row["current_dedup_threshold"]),
                "suggested_interval_seconds": float(row["suggested_interval_seconds"]),
                "suggested_dedup_threshold": int(row["suggested_dedup_threshold"]),
                "rationale": cast("str | None", row["rationale"]),
                "applied_at": cast("str | None", row["applied_at"]),
            },
        )
    return out


__all__ = [
    "apply_suggestion",
    "compute_advisor_state",
    "list_recent_runs",
    "record_advisor_run",
]
