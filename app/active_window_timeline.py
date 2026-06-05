"""24-hour active-window minute timeline — what app was on screen each minute.

For every minute of the trailing 24 hours we pick the screenshot whose
``captured_at`` is closest to that minute's centre and treat its
``app_name`` as the "active window" for that minute. Consecutive same-app
minutes collapse into a *run* so the SVG sparkline renders one ``<rect>``
per run instead of one per minute — both shapes are returned so callers
can pick the cheaper iteration for their layout.

The output is a plain ``dict`` (not a Pydantic model) because both the
HTML page and the ``/api/stats/active-window.json`` endpoint serialise
it straight to the wire — the structure is the public API.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.active_window_timeline")

# Spec: 24 hours x 60 minutes = 1440 buckets. Kept as named constants so
# the SQL bind value and the Python ``range`` stay in lock-step.
_HOURS = 24
_MINUTES_PER_HOUR = 60
_TOTAL_MINUTES = _HOURS * _MINUTES_PER_HOUR
_SECONDS_PER_MINUTE = 60

# Placeholder app name for minutes with no screenshot in range. Rendered
# in the SVG using the inert-zinc tone returned by :func:`_color_for`
# so empty stretches read as background.
_IDLE_APP_NAME = "—"

# HSL palette parameters. Saturation and lightness are fixed so every
# app lands at a similar visual weight; only hue varies per app via a
# stable hash. The idle/empty bucket gets its own dimmer slot.
_PALETTE_SATURATION_PCT = 62
_PALETTE_LIGHTNESS_PCT = 52
_IDLE_COLOR_HSL = "hsl(0, 0%, 18%)"


class MinuteBucket(TypedDict):
    minute_index: int
    app_name: str
    color: str


class AppRun(TypedDict):
    start_minute: int
    end_minute: int
    app_name: str
    duration_minutes: int
    color: str


class TopApp(TypedDict):
    app: str
    total_minutes: int
    color: str


class ActiveWindowTimeline(TypedDict):
    minutes: list[MinuteBucket]
    runs: list[AppRun]
    top_apps: list[TopApp]
    anchor_iso: str
    start_iso: str


def _color_for(app_name: str) -> str:
    """Return a stable HSL colour string for ``app_name``.

    The hash is BLAKE2b-truncated so changes to the Python ``hash()``
    seed across processes don't shuffle the palette between sessions.
    Empty / sentinel app names collapse to the dim idle colour so the
    SVG background reads as inert rather than as a real app.
    """
    if not app_name or app_name == _IDLE_APP_NAME:
        return _IDLE_COLOR_HSL
    digest = hashlib.blake2b(app_name.encode("utf-8"), digest_size=2).digest()
    hue = (digest[0] << 8 | digest[1]) % 360
    # Emit raw HSL so the SVG stays human-inspectable in DevTools; the
    # browser handles the colour-space conversion natively.
    return f"hsl({hue}, {_PALETTE_SATURATION_PCT}%, {_PALETTE_LIGHTNESS_PCT}%)"


def _parse_anchor(now_iso: str | None) -> datetime:
    """Return the timezone-aware UTC anchor for the 24h window.

    ``None`` (the production caller) maps to ``datetime.now(UTC)``.
    Naive ISO strings are assumed to already represent UTC — the storage
    layer only ever writes UTC, so the same convention applies here.
    """
    if now_iso is None:
        return datetime.now(tz=UTC)
    parsed = datetime.fromisoformat(now_iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _bucket_screenshots(
    rows: list[tuple[str, str]],
    start: datetime,
) -> list[str]:
    """Pick the closest-in-time ``app_name`` for each of the 1440 minutes.

    ``rows`` is ``[(app_name, captured_at_iso), ...]`` already ordered by
    ``captured_at`` ASC. For each minute we keep the row whose
    ``captured_at`` is closest to the minute's centre *and* falls
    strictly within the minute's [start, start+60s) window — anything
    outside the minute keeps that minute as idle. This matches the
    spec's "closest within that minute window" wording.
    """
    minute_app: list[str] = [_IDLE_APP_NAME] * _TOTAL_MINUTES
    # best_offset_s[i] tracks |captured_at - minute_centre| for the
    # currently-chosen row in minute i. ``None`` = still idle.
    best_offset_s: list[float | None] = [None] * _TOTAL_MINUTES

    for app_raw, captured_at_raw in rows:
        if not app_raw:
            continue
        try:
            when = datetime.fromisoformat(str(captured_at_raw))
        except ValueError:
            log.warning("active_window.bad_captured_at", value=str(captured_at_raw)[:32])
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        delta = (when - start).total_seconds()
        if delta < 0 or delta >= _TOTAL_MINUTES * _SECONDS_PER_MINUTE:
            continue
        idx = int(delta // _SECONDS_PER_MINUTE)
        if not 0 <= idx < _TOTAL_MINUTES:
            continue
        # Offset from the centre of minute ``idx``. Smaller is better.
        minute_centre_s = (idx + 0.5) * _SECONDS_PER_MINUTE
        offset = abs(delta - minute_centre_s)
        current = best_offset_s[idx]
        if current is None or offset < current:
            best_offset_s[idx] = offset
            minute_app[idx] = str(app_raw)

    return minute_app


def _build_runs(
    minute_app: list[str],
    color_cache: dict[str, str],
) -> list[AppRun]:
    """Collapse consecutive same-app minutes into runs."""
    runs: list[AppRun] = []
    if not minute_app:
        return runs
    run_start = 0
    run_app = minute_app[0]
    for idx in range(1, len(minute_app)):
        if minute_app[idx] == run_app:
            continue
        runs.append(
            AppRun(
                start_minute=run_start,
                end_minute=idx - 1,
                app_name=run_app,
                duration_minutes=idx - run_start,
                color=color_cache[run_app],
            )
        )
        run_start = idx
        run_app = minute_app[idx]
    runs.append(
        AppRun(
            start_minute=run_start,
            end_minute=len(minute_app) - 1,
            app_name=run_app,
            duration_minutes=len(minute_app) - run_start,
            color=color_cache[run_app],
        )
    )
    return runs


def _top_apps(
    minute_app: list[str],
    color_cache: dict[str, str],
) -> list[TopApp]:
    """Aggregate per-app minute totals, sorted descending by minutes.

    The idle/empty bucket is excluded so the legend lists only apps that
    actually appeared on screen.
    """
    totals: dict[str, int] = {}
    for app in minute_app:
        if app == _IDLE_APP_NAME:
            continue
        totals[app] = totals.get(app, 0) + 1
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        TopApp(app=app, total_minutes=minutes, color=color_cache[app])
        for app, minutes in ranked
    ]


async def build_24h_active(now_iso: str | None = None) -> ActiveWindowTimeline:
    """Return the 24-hour minute-by-minute active-window timeline.

    Pulls every screenshot whose ``captured_at`` falls in
    ``[anchor - 24h, anchor)`` from SQLite via a parametrised query,
    buckets each one into the minute it best fits, then emits a
    1440-entry dense series plus the same series collapsed into runs.

    The output also carries ``anchor_iso`` / ``start_iso`` so the
    template and the JSON endpoint can label the timeline axis without
    re-deriving the window inside Jinja.
    """
    anchor = _parse_anchor(now_iso)
    start = anchor - timedelta(hours=_HOURS)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, captured_at "
            "FROM screenshots "
            "WHERE captured_at IS NOT NULL "
            "AND captured_at >= ? "
            "AND captured_at < ? "
            "ORDER BY captured_at ASC",
            (start.isoformat(), anchor.isoformat()),
        )
        raw_rows = await cursor.fetchall()

    rows: list[tuple[str, str]] = [
        (str(row["app_name"]) if row["app_name"] is not None else "", str(row["captured_at"]))
        for row in raw_rows
    ]

    minute_app = _bucket_screenshots(rows, start)

    # Pre-compute one HSL string per distinct app (plus the idle bucket)
    # so the per-minute / per-run / per-top-app payloads all reuse the
    # same value without re-hashing 1440 times.
    color_cache: dict[str, str] = {_IDLE_APP_NAME: _IDLE_COLOR_HSL}
    for app in minute_app:
        if app not in color_cache:
            color_cache[app] = _color_for(app)

    minutes: list[MinuteBucket] = [
        MinuteBucket(
            minute_index=idx,
            app_name=app,
            color=color_cache[app],
        )
        for idx, app in enumerate(minute_app)
    ]
    runs = _build_runs(minute_app, color_cache)
    top_apps = _top_apps(minute_app, color_cache)

    payload: ActiveWindowTimeline = {
        "minutes": minutes,
        "runs": runs,
        "top_apps": top_apps,
        "anchor_iso": anchor.isoformat(),
        "start_iso": start.isoformat(),
    }

    log.info(
        "active_window.computed",
        anchor=anchor.isoformat(),
        rows=len(rows),
        runs=len(runs),
        distinct_apps=len(top_apps),
        idle_minutes=sum(1 for app in minute_app if app == _IDLE_APP_NAME),
    )

    return payload
