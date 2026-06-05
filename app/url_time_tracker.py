"""Best-effort per-page time tracking for browser screenshots (v1.50).

The user wants to know "where did my browser time go today?".  The
hard answer — real per-URL attribution — would need a browser
extension; Persona is screenshot-only.  This module makes the best
compromise the data allows:

1. We treat the four major desktop browsers as a small denylist of
   ``app_name`` values: Chrome / Firefox / Safari / Edge.  Anything else
   skips this aggregator entirely.
2. For each browser screenshot we strip the trailing ``" — Google Chrome"``
   (or sibling) suffix from the window title, lower-case it, truncate
   to 80 chars, and treat the result as a "page label".  Two visits to
   the same URL with the same page title therefore land in the same
   bucket; two pages with the same title (e.g. "GitHub") collapse;
   one page with two different titles (e.g. a chat that updates the
   tab title) splits.  All of this is acceptable for a coarse "where
   did my browser time go?" view; the alternative is no view at all.
3. We multiply the bucket's screenshot count by the current
   ``capture_interval_seconds`` setting to estimate elapsed seconds.
   This is a static estimate — the worker recomputes the whole row on
   every tick so a drifting capture cadence just changes what the
   *next* recompute writes, not the historical row.

Everything is async and parametrised; the public surface is
``BROWSER_APPS``, :func:`extract_page_label`, and
:func:`aggregate_day`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.settings.effective import get_effective_float
from app.storage.db import get_connection

log = get_logger("persona.url_time_tracker")


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


BROWSER_APPS: Final[frozenset[str]] = frozenset(
    {
        "google chrome",
        "chrome",
        "chromium",
        "mozilla firefox",
        "firefox",
        "safari",
        "microsoft edge",
        "edge",
    }
)

# Trailing suffixes the desktop browsers append to the active tab title.
# Order matters only insofar as we strip the *longest* match first so
# "Microsoft Edge" doesn't swallow a stray "Edge" inside the page title.
_TITLE_SUFFIXES: Final[tuple[str, ...]] = (
    " — Google Chrome",
    " - Google Chrome",
    " — Chromium",
    " - Chromium",
    " — Mozilla Firefox",
    " - Mozilla Firefox",
    " — Microsoft Edge",
    " - Microsoft Edge",
    " — Edge",
    " - Edge",
    " — Safari",
    " - Safari",
)

_MAX_LABEL_LENGTH: Final[int] = 80

# Fallback capture cadence when the kv/Settings layer is unreadable.
_FALLBACK_CAPTURE_INTERVAL: Final[float] = 6.0


# ---------------------------------------------------------------------------
# Page-label extractor
# ---------------------------------------------------------------------------


def extract_page_label(app_name: str | None, window_title: str | None) -> str | None:
    """Return a lowercase truncated page label for browser screenshots.

    Returns ``None`` when ``app_name`` is not a known browser or when
    ``window_title`` is empty after stripping.  Non-browser apps are
    skipped entirely so the caller can use a single helper to filter
    rows before aggregating.
    """
    if not app_name or not window_title:
        return None
    if app_name.strip().lower() not in BROWSER_APPS:
        return None

    title = window_title.strip()
    if not title:
        return None

    # Strip the longest matching browser suffix.  We iterate sorted by
    # descending length so "Microsoft Edge" outranks "Edge" when both
    # would otherwise match.
    for suffix in sorted(_TITLE_SUFFIXES, key=len, reverse=True):
        if title.endswith(suffix):
            title = title[: -len(suffix)].rstrip()
            break

    if not title:
        return None

    truncated = title[:_MAX_LABEL_LENGTH]
    return truncated.lower()


# ---------------------------------------------------------------------------
# Day aggregator
# ---------------------------------------------------------------------------


class _AggregateSummary(TypedDict):
    day: str
    rows_written: int
    total_screen_count: int
    total_est_seconds: int


def _normalise_day(day_iso: str) -> str:
    """Validate ``YYYY-MM-DD``; fall back to today on bad input."""
    try:
        return date.fromisoformat(day_iso).isoformat()
    except ValueError:
        log.warning("url_time.bad_day_iso", day_iso=day_iso)
        return datetime.now().astimezone().date().isoformat()


async def aggregate_day(day_iso: str) -> _AggregateSummary:
    """Recompute ``url_time_aggregate`` rows for ``day_iso``.

    The function is fully idempotent — re-running on the same day
    overwrites the ``screen_count`` / ``est_seconds`` / ``computed_at``
    of any existing rows.  Returning a small dict lets the worker log
    a single ``worker.cycle`` line with meaningful counters.
    """
    day = _normalise_day(day_iso)
    interval = await _resolve_capture_interval()

    buckets: dict[tuple[str, str], int] = {}

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, window_title "
            "FROM screenshots "
            "WHERE DATE(captured_at) = ? "
            "AND app_name IS NOT NULL "
            "AND window_title IS NOT NULL",
            (day,),
        )
        rows = await cursor.fetchall()

        for row in rows:
            raw_app = row["app_name"]
            raw_title = row["window_title"]
            label = extract_page_label(
                str(raw_app) if raw_app is not None else None,
                str(raw_title) if raw_title is not None else None,
            )
            if label is None:
                continue
            browser = str(raw_app).strip()
            key = (browser, label)
            buckets[key] = buckets.get(key, 0) + 1

        rows_written = 0
        total_count = 0
        total_seconds = 0
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")

        for (browser, page_label), screen_count in buckets.items():
            est_seconds = round(screen_count * interval)
            await conn.execute(
                "INSERT INTO url_time_aggregate "
                "(day, browser, page_label, screen_count, est_seconds, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(day, browser, page_label) DO UPDATE SET "
                "screen_count = excluded.screen_count, "
                "est_seconds = excluded.est_seconds, "
                "computed_at = excluded.computed_at",
                (day, browser, page_label, screen_count, est_seconds, now_iso),
            )
            rows_written += 1
            total_count += screen_count
            total_seconds += est_seconds

        await conn.commit()

    summary: _AggregateSummary = {
        "day": day,
        "rows_written": rows_written,
        "total_screen_count": total_count,
        "total_est_seconds": total_seconds,
    }
    log.info(
        "url_time.aggregated",
        day=day,
        rows_written=rows_written,
        total_screen_count=total_count,
        total_est_seconds=total_seconds,
        capture_interval=interval,
    )
    return summary


async def _resolve_capture_interval() -> float:
    """Read the live capture cadence; fall back if the resolver fails."""
    try:
        return await get_effective_float(
            "capture_interval_seconds",
            default=_FALLBACK_CAPTURE_INTERVAL,
        )
    except Exception as exc:
        log.debug("url_time.interval_lookup_failed", error=str(exc))
        return _FALLBACK_CAPTURE_INTERVAL


__all__ = [
    "BROWSER_APPS",
    "aggregate_day",
    "extract_page_label",
]
