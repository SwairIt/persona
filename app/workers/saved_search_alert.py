"""Saved-search alert worker — fire a webhook when bookmarked queries match new shots.

Each row in ``saved_search`` (migration 025) holds a user-pinned FTS
query. This worker polls every five minutes, re-runs each query bounded
to screenshots inserted since the row's ``last_checked_at`` watermark,
and dispatches a ``saved_search.matched`` webhook event whenever the
count of new hits is greater than zero. The watermark is then advanced
to the moment the poll started so the same row never refires for the
same shots.

Idempotency: the watermark is the load-bearing piece. We snapshot
``datetime.now(UTC)`` *before* running any query, then write that exact
value into ``last_checked_at`` after the webhook is dispatched. A crash
between query and write leaves the old watermark in place, which means
the next tick will replay the same window — the receiver may see a
duplicate event, but a missed event is impossible. We accept duplicate
delivery over silent loss; the webhooks contract has never promised
exactly-once.

First-run behaviour: a fresh bookmark (``last_checked_at IS NULL``)
adopts the current tick's timestamp without firing any webhook. Without
this we would flood the receiver with the entire screenshot backlog the
first time the user enables the integration.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from app.logging_setup import get_logger
from app.search.queries import search
from app.storage.db import get_connection
from app.storage.time import iso, parse_iso
from app.webhooks import dispatch_event
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.saved_search.alert")

POLL_INTERVAL_SECONDS: float = 300.0
"""Five-minute polling cadence — small enough to feel like an alert,
large enough that the FTS scan never piles up behind itself."""

EVENT_TYPE: str = "saved_search.matched"
"""Webhook event type fired for every saved search that produced new hits."""

_MATCH_LIMIT: int = 50
"""Upper bound on hits sent inside the webhook payload. Receivers that
need the full list can re-run the bookmark query via the HTTP API; the
payload only carries enough context for routing/notification."""


async def run_saved_search_alert_worker(
    controller: CaptureController | None = None,
) -> None:
    """Continuously poll saved searches until ``stop_event`` fires.

    The loop tolerates per-tick failures: an exception while polling one
    bookmark is logged and the loop continues with the next row, so a
    single malformed query never silences the whole worker.
    """
    ctrl = controller or get_controller()
    log.info("saved_search.alert.started", poll_seconds=POLL_INTERVAL_SECONDS)

    while not ctrl.stop_event.is_set():
        await beat("saved-search-alert")
        try:
            await _poll_once()
        except asyncio.CancelledError:
            log.info("saved_search.alert.cancelled")
            raise
        except Exception as exc:
            log.exception("saved_search.alert.tick_failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue

    log.info("saved_search.alert.stopped")


async def _poll_once() -> None:
    """Walk every saved search; fire webhooks for the ones with new hits.

    Snapshots ``now`` at the top of the call so every bookmark uses the
    same upper bound for "new since previous tick" — otherwise a slow
    FTS scan would leave a sliver of time uncovered between rows.
    """
    now = datetime.now(UTC)
    rows = await _load_saved_searches()
    if not rows:
        return

    for row in rows:
        slug = row["slug"]
        query = row["query"]
        last_checked_raw = row["last_checked_at"]

        try:
            since = _parse_watermark(last_checked_raw)
        except ValueError as exc:
            log.warning(
                "saved_search.alert.bad_watermark",
                slug=slug,
                value=last_checked_raw,
                error=str(exc),
            )
            since = None

        if since is None:
            # First sighting: adopt the current tick as the watermark and
            # skip firing — we don't replay the backlog the user already
            # has in their UI.
            await _advance_watermark(slug, now)
            log.info("saved_search.alert.bootstrap", slug=slug, watermark=iso(now))
            continue

        try:
            hits = await _run_query(query=query, since=since)
        except Exception as exc:
            log.exception(
                "saved_search.alert.query_failed",
                slug=slug,
                error=str(exc),
            )
            continue

        if hits:
            payload = _build_payload(row=row, since=since, until=now, hits=hits)
            await dispatch_event(EVENT_TYPE, payload)
            log.info(
                "saved_search.alert.fired",
                slug=slug,
                match_count=len(hits),
                since=iso(since),
                until=iso(now),
            )
        else:
            log.debug(
                "saved_search.alert.no_matches",
                slug=slug,
                since=iso(since),
                until=iso(now),
            )

        await _advance_watermark(slug, now)


async def _load_saved_searches() -> list[dict[str, Any]]:
    """Snapshot every bookmark row into plain dicts.

    We don't hold the connection across the FTS scans below — each query
    opens its own short-lived connection so a slow saved search can't
    starve other writers. Returning dicts (not aiosqlite ``Row``s)
    decouples the worker from the connection's lifetime.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT slug, title, query, created_at, last_checked_at "
            "FROM saved_search ORDER BY created_at ASC",
        )
        rows = await cursor.fetchall()

    return [
        {
            "slug": str(row["slug"]),
            "title": str(row["title"]),
            "query": str(row["query"]),
            "created_at": str(row["created_at"]),
            "last_checked_at": (
                None
                if row["last_checked_at"] is None
                else str(row["last_checked_at"])
            ),
        }
        for row in rows
    ]


def _parse_watermark(raw: str | None) -> datetime | None:
    """Parse the watermark column. ``None`` means "never polled before"."""
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    parsed = parse_iso(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _run_query(
    *,
    query: str,
    since: datetime,
) -> list[dict[str, Any]]:
    """Re-run the bookmarked FTS query bounded to shots inserted since ``since``.

    We deliberately reuse :func:`app.search.queries.search` rather than
    re-implementing the FTS plumbing — that keeps the dialect, sanitiser
    and ranking identical to what the user sees in the UI. The ``since``
    bound is the same ``captured_at >= ?`` clause that the search route
    already exposes, so a bookmark and an interactive search agree on
    which rows are "new".
    """
    async with get_connection() as conn:
        hits = await search(
            conn,
            query=query,
            since=since,
            limit=_MATCH_LIMIT,
        )

    return [
        {
            "screenshot_id": hit.screenshot_id,
            "captured_at": iso(hit.captured_at),
            "app_name": hit.app_name,
            "window_title": hit.window_title,
            "snippet": hit.snippet,
        }
        for hit in hits
    ]


def _build_payload(
    *,
    row: dict[str, Any],
    since: datetime,
    until: datetime,
    hits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the JSON payload posted to webhook subscribers."""
    return {
        "slug": row["slug"],
        "title": row["title"],
        "query": row["query"],
        "match_count": len(hits),
        "window_since": iso(since),
        "window_until": iso(until),
        "hits": hits,
    }


async def _advance_watermark(slug: str, watermark: datetime) -> None:
    """Write the new ``last_checked_at`` value for one bookmark.

    Always uses a parametrised query — the slug came from the database
    row, but we still bind it rather than interpolating, so the path can
    never grow a SQL-injection regression if the column ever stops being
    constrained by the route validator.
    """
    try:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE saved_search SET last_checked_at = ? WHERE slug = ?",
                (iso(watermark), slug),
            )
            await conn.commit()
    except aiosqlite.Error as exc:
        log.warning(
            "saved_search.alert.watermark_write_failed",
            slug=slug,
            error=str(exc),
        )


__all__ = ["EVENT_TYPE", "POLL_INTERVAL_SECONDS", "run_saved_search_alert_worker"]
