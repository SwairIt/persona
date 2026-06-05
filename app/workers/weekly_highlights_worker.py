"""Weekly LLM highlights worker (v1.46).

Polls every hour. When the operator has opted in via
``kv_settings.weekly_highlights_enabled = '1'`` the worker looks back
four ISO weeks and asks :func:`app.llm.weekly_highlights.generate_highlights`
to fill the ``weekly_highlight`` table for any week that does not yet
have any picks.

Disabled by default — the kv flag is absent on a fresh install, which
this module treats as ``'0'``. Flipping the flag to ``'1'`` is the only
way to start spending tokens on the curated weekly highlights.

The worker is a thin :class:`BackfillRunner` wrapper, mirroring
:mod:`app.workers.weekly_rollup_worker`. The lifespan task list in
:mod:`app.web.main` is intentionally NOT touched here — wiring is done
in a follow-up patch.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from app.llm.weekly_highlights import generate_highlights
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.weekly_highlights_worker")

POLL_INTERVAL_SECONDS: int = 3600
LOOKBACK_WEEKS: int = 4
_KV_FLAG: str = "weekly_highlights_enabled"
_WORKER_NAME: str = "weekly-highlights-worker"


async def _is_enabled() -> bool:
    """Return ``True`` only when the kv row is the literal string ``'1'``."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_FLAG)
    return (raw or "0").strip() == "1"


def _monday_of(when: date) -> date:
    """Return the Monday of the ISO week containing ``when``."""
    return when - timedelta(days=when.weekday())


def _recent_week_starts(count: int) -> list[str]:
    """Return ``count`` ISO ``YYYY-MM-DD`` strings, current week first.

    Walks backwards in 7-day strides from the Monday of the current
    UTC week so the most recent week is always tried first; freshly
    enabled installs see the user's *current* week's picks before the
    backfill chews through older weeks.
    """
    current_monday = _monday_of(datetime.now(tz=UTC).date())
    return [
        (current_monday - timedelta(days=7 * i)).isoformat()
        for i in range(count)
    ]


async def _select_pending() -> list[str]:
    """Return week_start strings still missing any highlights.

    Returns an empty list when the feature is disabled — the
    :class:`BackfillRunner` then just sleeps until the next tick. The
    SQL is parametrised: for each candidate week we ask whether
    ``COUNT(weekly_highlight) = 0``; only weeks that satisfy that are
    returned, newest first.
    """
    if not await _is_enabled():
        return []

    candidates = _recent_week_starts(LOOKBACK_WEEKS)
    pending: list[str] = []
    async with get_connection() as conn:
        for week_start_iso in candidates:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS c FROM weekly_highlight "
                "WHERE week_start = ?",
                (week_start_iso,),
            )
            row = await cursor.fetchone()
            existing = int(row["c"]) if row is not None else 0
            if existing == 0:
                pending.append(week_start_iso)
    return pending


async def _build_one(week_start_iso: str) -> dict[str, object]:
    """Wrap :func:`generate_highlights` so the runner sees ``{}`` on no-ops.

    The runner increments its built-counter only on truthy results, so
    we return ``{}`` for status-skips (``missing_config``, ``no_data``,
    ``already_done``, ``error``) and the real dict only on actual work.
    """
    result = await generate_highlights(week_start_iso)
    status = result["status"]
    if status == "missing_config":
        log.info(
            "weekly_highlights_worker.missing_config",
            week_start=week_start_iso,
        )
        return {}
    if status in {"already_done", "no_data", "error"}:
        return {}
    return dict(result)


async def run_weekly_highlights_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name=_WORKER_NAME,
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_select_pending,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = [
    "LOOKBACK_WEEKS",
    "POLL_INTERVAL_SECONDS",
    "run_weekly_highlights_worker",
]
