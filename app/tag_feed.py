"""RSS builder for ``/feeds/tag/{tag}.rss`` — subscribe to a specific tag.

Why this exists
---------------
Persona's tag surface is rich on the HTML side (``/tags``,
``/tags/{id}``, the tag-gallery, the tag-tree) but the existing RSS
exposure is per-collection or per-saved-search — not per *raw tag*.
A power user who has been hand-tagging shots ``#work-recipe`` or
``#idea`` wants to point a feed reader at "every shot I drop into
this bucket" without first wrapping the tag in an auto-collection
rule. This module renders that feed.

It is the first-class sibling of :mod:`app.pinned_feed`: same shape,
same XML safety contract, just keyed on a tag name instead of the
``pinned_at`` column. There is a *second* per-tag RSS endpoint at
``/feeds/tags/{tag_name}.rss`` in :mod:`app.web.routes.rss`; that one
lives behind the legacy ``/feeds`` router and resolves through
``get_screenshot`` row by row. This module is the leaner, dedicated
implementation behind ``/feeds/tag/{tag}.rss`` — one query, no
per-row repository round-trip, matching the pinned-feed pattern so
the auth wrapper and the OPML entry have a clean mirror to copy.

Schema note
-----------
``screenshot_tags`` is a join table — the column is ``tag_id``, not
``tag`` or ``tag_name`` (see ``migrations/001_tags.sql``). The
canonical tag name lives in ``tags.name``. The query therefore JOINs
through ``tags`` rather than filtering ``screenshot_tags`` directly.

Design contract
---------------
* **Most-recent first.** ``ORDER BY s.captured_at DESC LIMIT 50``;
  ``LIMIT`` is parametrised so no integer interpolation hits the SQL
  string, matching the rest of :mod:`app.storage`.
* **Description = alt_text when present, else OCR snippet.** Same
  fall-back ladder as :mod:`app.pinned_feed`.
* **Tag-name matching is case-insensitive.** ``LOWER(t.name) = LOWER(?)``
  so ``/feeds/tag/Work-Recipe.rss`` and ``/feeds/tag/work-recipe.rss``
  return the same shots — the operator may type tags either way in
  their reader. We surface the canonical casing from the ``tags``
  table in the channel title so the feed reader UI matches reality.
* **XML-safe.** Every dynamic field (tag, app, window-title, alt-text,
  OCR snippet, timestamps, URLs) goes through
  :func:`xml.sax.saxutils.escape` before it touches the response body.
* **Parametrised SQL only.** Tag value, limit, everything bound — the
  project rule, not a CVE-driven one.
* **Empty result returns ``""``.** The HTTP wrapper turns that into a
  ``404`` so feed readers stop polling dead tags instead of silently
  subscribing to an empty channel.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from xml.sax.saxutils import escape as xml_escape

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.tag_feed")


# Spec: first 300 characters of ``ocr_text`` when ``alt_text`` is
# missing. Same constant value as :mod:`app.pinned_feed` so both
# feeds render the same body length for a given shot.
_OCR_SNIPPET_LEN = 300


async def build_tag_rss(tag: str, host: str, limit: int = 50) -> str:
    """Render the RSS 2.0 document for one tag's most-recent shots.

    Args:
        tag: The tag name to filter by. Matched case-insensitively
            against ``tags.name``; the canonical casing from the DB
            is used in the channel title and self-link.
        host: Origin to prefix every relative link with — typically
            ``http://{settings.host}:{settings.port}``. A trailing
            slash is tolerated and stripped.
        limit: Maximum number of shots to include. The HTTP wrapper
            passes ``50`` per the spec; tests may pass a smaller
            value to exercise the empty / single-item paths.

    Returns:
        A complete RSS 2.0 XML document as a string, ready to be sent
        with ``Content-Type: application/rss+xml; charset=utf-8``.
        Returns the empty string when the tag has zero matching rows
        so the HTTP wrapper can map that to a ``404``.
    """
    base = host.rstrip("/")

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT s.id, s.captured_at, s.app_name, s.window_title,
                   s.ocr_text, s.alt_text, t.name AS tag_name
            FROM screenshots s
            JOIN screenshot_tags st ON st.screenshot_id = s.id
            JOIN tags t ON t.id = st.tag_id
            WHERE LOWER(t.name) = LOWER(?)
            ORDER BY s.captured_at DESC
            LIMIT ?
            """,
            (tag, int(limit)),
        )
        rows = list(await cursor.fetchall())

    if not rows:
        log.info("tag_feed.empty", tag=tag)
        return ""

    canonical_name = str(rows[0]["tag_name"])
    items_xml = [_render_item(row, base) for row in rows]
    joined_items = "\n".join(items_xml)
    last_build = format_datetime(datetime.now(UTC))

    self_link = xml_escape(f"{base}/feeds/tag/{canonical_name}.rss")
    page_link = xml_escape(f"{base}/tag/{canonical_name}")

    channel_title = f"Persona — tag #{canonical_name}"
    channel_desc = (
        f"Most-recent shots tagged #{canonical_name} on this Persona instance "
        "— newest first."
    )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{xml_escape(channel_title)}</title>\n"
        f"    <link>{page_link}</link>\n"
        f'    <atom:link href="{self_link}" rel="self" '
        'type="application/rss+xml" />\n'
        f"    <description>{xml_escape(channel_desc)}</description>\n"
        f"    <lastBuildDate>{last_build}</lastBuildDate>\n"
        f"{joined_items}\n"
        "  </channel>\n"
        "</rss>\n"
    )

    log.info(
        "tag_feed.built",
        tag=canonical_name,
        items=len(items_xml),
        bytes=len(body),
    )
    return body


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_item(row: object, base: str) -> str:
    """Render a single ``<item>`` element for one tagged screenshot row.

    ``row`` is typed as :class:`object` because :mod:`aiosqlite`'s
    ``Row`` class isn't exported in a way mypy-strict can latch onto;
    we coerce every column explicitly through ``str`` / ``int`` below
    so the runtime contract is the same regardless of the static type.
    """
    sid = int(row["id"])  # type: ignore[index]
    captured_at_raw = str(row["captured_at"])  # type: ignore[index]
    app_name = str(row["app_name"] or "Unknown app")  # type: ignore[index]
    window_title = str(row["window_title"] or "")  # type: ignore[index]
    alt_text_raw = row["alt_text"]  # type: ignore[index]
    ocr_text_raw = row["ocr_text"]  # type: ignore[index]
    tag_name = str(row["tag_name"])  # type: ignore[index]

    captured_dt = _parse_iso_utc(captured_at_raw)
    captured_label = captured_dt.strftime("%Y-%m-%d %H:%M")

    title = f"{captured_label} — {app_name} — {window_title}".rstrip(" —")

    # Spec: alt_text when present, else first 300 chars of ocr_text.
    # Treat ``alt_text = ''`` the same as NULL so an empty cell still
    # falls back to OCR rather than rendering a blank description.
    description = ""
    if alt_text_raw is not None and str(alt_text_raw).strip():
        description = str(alt_text_raw).strip()
    elif ocr_text_raw is not None and str(ocr_text_raw).strip():
        description = str(ocr_text_raw).strip()[:_OCR_SNIPPET_LEN]

    link_url = f"{base}/screenshot/{sid}"
    # Spec: stable guid ``persona-tag-{tag}-{shot_id}``.
    guid_value = f"persona-tag-{tag_name}-{sid}"
    pub_date = format_datetime(captured_dt)

    return (
        "    <item>\n"
        f"      <title>{xml_escape(title)}</title>\n"
        f"      <link>{xml_escape(link_url)}</link>\n"
        f'      <guid isPermaLink="false">{xml_escape(guid_value)}</guid>\n'
        f"      <pubDate>{pub_date}</pubDate>\n"
        f"      <description>{xml_escape(description)}</description>\n"
        "    </item>"
    )


def _parse_iso_utc(value: str) -> datetime:
    """Parse a ``captured_at`` ISO-8601 string, attaching UTC if naive.

    Most rows are written by :mod:`app.capture` as UTC-aware ISO
    strings, but a row imported from an older path may be naive; we
    attach UTC explicitly so :func:`email.utils.format_datetime`
    emits a valid RFC-822 ``pubDate`` either way. A truly malformed
    value falls back to *now* so the feed still validates rather
    than raising mid-render.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.warning("tag_feed.bad_ts", ts=value)
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


__all__ = ["build_tag_rss"]
