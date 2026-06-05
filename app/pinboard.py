"""Pinboard masonry — paginated view of every pinned screenshot.

Why this exists
---------------
The existing :mod:`app.pinmap` page groups every pinned shot into
``YYYY-MM`` clusters and renders a strict 4-column grid. That works
when an operator has tens of pins but turns into a wall of identical
crops once the collection grows; the natural-feeling alternative is
the Pinterest-style fluid masonry — a CSS-columns layout that lets
each card keep its own aspect ratio so the eye can scan the board
visually rather than row-by-row.

This module supplies the data side of that surface: a single async
helper that returns one page of pinned shots ordered by ``pinned_at``
(newest first), plus an optional tag filter. The shape it returns is
deliberately HTMX-friendly — ``has_more`` together with the next
``offset`` is everything the template needs to render the
"load-more-on-scroll" sentinel without the route having to do extra
math.

Design contract
---------------
* **``pinned_at IS NOT NULL`` is the source of truth.** Same as
  :mod:`app.pinned_feed` (the RSS sibling) — a row counts as pinned
  iff the column is populated, regardless of which subsystem
  (manual pin, auto-pin engine, daily-pin) wrote it.
* **Newest-pin-first.** ``ORDER BY pinned_at DESC`` matches the RSS
  feed and the "Pinned recently" header on the timeline; consistent
  ordering across surfaces matters more than picking the "right"
  one.
* **Tag filter is a normal INNER JOIN against ``screenshot_tags``.**
  No subquery, no IN-list, no FTS — the catalog is small (one row per
  pin) and a straight join is the simplest correct shape. The ``tag``
  parameter is bound — never interpolated — so even a tag name with
  ``%`` or ``;`` in it is safe.
* **Pagination is offset-based, not keyset.** Pin count is bounded by
  human attention; an offset scan is fine here and matches the rest
  of the codebase (e.g. ``/api/recent``). ``limit`` is clamped to a
  sane upper bound so a hostile ``?limit=999999`` can't drag the
  process down.
* **Parametrised SQL only.** Project rule — every dynamic value is a
  ``?`` placeholder. The only literal in the SQL is the table /
  column list.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.routes.thumbnails import thumbnail_url

log = get_logger("persona.pinboard")


# Spec accepts any ``limit``/``offset``; we still clamp the upper bound
# so a runaway query string can't accidentally pull tens of thousands
# of rows. ``500`` is generous — the masonry will keep scrolling well
# before a single page ever fills up.
_MAX_LIMIT = 500
_MIN_LIMIT = 1
_MIN_OFFSET = 0


class PinboardItem(dict[str, Any]):
    """One pinned shot as it appears on the masonry page.

    Declared as a ``dict`` subclass rather than a :class:`TypedDict`
    so the JSON endpoint can serialise it directly and so a Jinja
    template can reach fields via the standard ``shot.id`` /
    ``shot['id']`` dotted access without a model conversion.

    Keys (all present, even when ``None``):

    * ``id`` — primary key of the row in ``screenshots``.
    * ``captured_at`` — ISO-8601 UTC timestamp of the capture.
    * ``app_name`` — foreground app at capture time, or ``None``.
    * ``window_title`` — foreground window title, or ``None``.
    * ``thumbnail_url`` — ``/thumbs/...`` URL or ``None`` when the
      WebP is missing.
    * ``pinned_at`` — ISO-8601 UTC timestamp the pin was recorded.
    """


def _clamp(value: int, lo: int, hi: int) -> int:
    """Clamp ``value`` into ``[lo, hi]`` — internal pagination guard."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


async def list_pinned(
    limit: int = 100,
    offset: int = 0,
    tag: str | None = None,
) -> dict[str, Any]:
    """Return one page of pinned screenshots, newest-pin-first.

    Args:
        limit: Maximum rows per page. Clamped to ``[1, 500]``.
        offset: Row offset for pagination. Clamped to ``>= 0``.
        tag: When provided, restrict the page to shots carrying the
            named tag via an INNER JOIN on ``screenshot_tags`` /
            ``tags``. ``None`` (the default) returns every pin.

    Returns:
        ``{"items": [...], "total": int, "offset": int, "limit": int,
        "has_more": bool}``. ``items`` is a list of :class:`PinboardItem`
        dicts, ``total`` is the unbounded count of pins matching the
        same filter, and ``has_more`` is the cheap "is there another
        page" hint the HTMX sentinel relies on.
    """
    safe_limit = _clamp(int(limit), _MIN_LIMIT, _MAX_LIMIT)
    safe_offset = max(_MIN_OFFSET, int(offset))
    normalised_tag = tag.strip() if isinstance(tag, str) else None
    if normalised_tag == "":
        normalised_tag = None

    async with get_connection() as conn:
        if normalised_tag is None:
            count_cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM screenshots "
                "WHERE pinned_at IS NOT NULL",
            )
            count_row = await count_cursor.fetchone()
            total = int(count_row["n"]) if count_row is not None else 0

            cursor = await conn.execute(
                """
                SELECT id, captured_at, app_name, window_title,
                       thumbnail_path, pinned_at
                FROM screenshots
                WHERE pinned_at IS NOT NULL
                ORDER BY pinned_at DESC
                LIMIT ? OFFSET ?
                """,
                (safe_limit, safe_offset),
            )
            rows = await cursor.fetchall()
        else:
            count_cursor = await conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM screenshots s
                JOIN screenshot_tags st ON st.screenshot_id = s.id
                JOIN tags t ON t.id = st.tag_id
                WHERE s.pinned_at IS NOT NULL AND t.name = ?
                """,
                (normalised_tag,),
            )
            count_row = await count_cursor.fetchone()
            total = int(count_row["n"]) if count_row is not None else 0

            cursor = await conn.execute(
                """
                SELECT s.id AS id,
                       s.captured_at AS captured_at,
                       s.app_name AS app_name,
                       s.window_title AS window_title,
                       s.thumbnail_path AS thumbnail_path,
                       s.pinned_at AS pinned_at
                FROM screenshots s
                JOIN screenshot_tags st ON st.screenshot_id = s.id
                JOIN tags t ON t.id = st.tag_id
                WHERE s.pinned_at IS NOT NULL AND t.name = ?
                ORDER BY s.pinned_at DESC
                LIMIT ? OFFSET ?
                """,
                (normalised_tag, safe_limit, safe_offset),
            )
            rows = await cursor.fetchall()

    items: list[PinboardItem] = []
    for row in rows:
        thumb_raw = row["thumbnail_path"]
        thumb_url = thumbnail_url(str(thumb_raw)) if thumb_raw is not None else None
        app_raw = row["app_name"]
        window_raw = row["window_title"]
        item = PinboardItem(
            id=int(row["id"]),
            captured_at=str(row["captured_at"]) if row["captured_at"] is not None else "",
            app_name=str(app_raw) if app_raw is not None else None,
            window_title=str(window_raw) if window_raw is not None else None,
            thumbnail_url=thumb_url,
            pinned_at=str(row["pinned_at"]) if row["pinned_at"] is not None else "",
        )
        items.append(item)

    has_more = (safe_offset + len(items)) < total

    log.info(
        "pinboard.listed",
        items=len(items),
        total=total,
        offset=safe_offset,
        limit=safe_limit,
        tag=normalised_tag,
        has_more=has_more,
    )

    return {
        "items": items,
        "total": total,
        "offset": safe_offset,
        "limit": safe_limit,
        "has_more": has_more,
    }


__all__ = ["PinboardItem", "list_pinned"]
