"""RSS builder for ``/feeds/pinned.rss`` — subscribe to your own pinned shots.

Why this exists
---------------
``screenshots.pinned_at`` lets a power user mark a moment as
important — sometimes manually from the gallery, sometimes via the
:mod:`app.auto_pin_engine` rules. The HTML surface for browsing pins
already exists (``/pinmap``), but a *passive* surface — "tell me when
something new gets pinned" — is missing. Mirroring the existing
:mod:`app.web.routes.audit_rss` / ``/feeds/journal.rss`` shape, this
module renders an RSS 2.0 document of the most-recent pins so a feed
reader can poll and surface each new pin as it happens.

Design contract
---------------
* **Most-recent first.** Spec says ``ORDER BY pinned_at DESC LIMIT 50``
  with one ``<item>`` per pin. ``LIMIT`` is parametrised even though
  the value is an ``int`` — keeping every value out of the SQL string
  is the project rule, not a CVE-driven one.
* **Description = alt_text when present, else OCR snippet.** A pinned
  shot is worth reading at a glance from a feed reader; the cached
  alt-text (when the operator has generated one) is the best one-line
  summary we have. When it isn't there we fall back to the first 300
  characters of ``ocr_text`` — enough to tell the operator what the
  shot was about without bloating the feed body.
* **XML-safe.** Every dynamic field (app, window-title, alt-text,
  OCR snippet, timestamps, URLs) goes through
  :func:`xml.sax.saxutils.escape` before it touches the response body.
  We treat OCR text as untrusted: it's whatever happened to be on
  screen at capture time and can contain anything from ``&`` to
  arbitrary unicode that needs escaping for valid XML.
* **Parametrised SQL only.** No format-string SQL — mirrors the
  project rule and matches the rest of :mod:`app.storage`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from xml.sax.saxutils import escape as xml_escape

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.pinned_feed.builder")


# Spec: first 300 characters of ``ocr_text`` when ``alt_text`` is
# missing. The constant keeps the snippet length documented at the
# call site so a future tuning is a one-line change.
_OCR_SNIPPET_LEN = 300


async def build_pinned_rss(host: str, limit: int = 50) -> str:
    """Render the RSS 2.0 document for the most-recent pinned shots.

    Args:
        host: Origin to prefix every relative link with — typically
            ``http://{settings.host}:{settings.port}``. A trailing
            slash is tolerated and stripped.
        limit: Maximum number of pinned shots to include. The HTTP
            wrapper passes ``50`` per the spec; tests may pass a
            smaller value to exercise the empty / single-item paths.

    Returns:
        A complete RSS 2.0 XML document as a string, ready to be sent
        with ``Content-Type: application/rss+xml; charset=utf-8``.
    """
    base = host.rstrip("/")

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, captured_at, pinned_at, app_name, window_title,
                   ocr_text, alt_text
            FROM screenshots
            WHERE pinned_at IS NOT NULL
            ORDER BY pinned_at DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        rows = await cursor.fetchall()

    items_xml = [_render_item(row, base) for row in rows]
    joined_items = "\n".join(items_xml)
    last_build = format_datetime(datetime.now(UTC))

    self_link = xml_escape(f"{base}/feeds/pinned.rss")
    page_link = xml_escape(f"{base}/pinmap")

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        "    <title>Persona Pinned Shots</title>\n"
        f"    <link>{page_link}</link>\n"
        f'    <atom:link href="{self_link}" rel="self" '
        'type="application/rss+xml" />\n'
        "    <description>Your pinned moments on this Persona instance "
        "— most-recent pins first.</description>\n"
        f"    <lastBuildDate>{last_build}</lastBuildDate>\n"
        f"{joined_items}\n"
        "  </channel>\n"
        "</rss>\n"
    )

    log.info("pinned_feed.built", items=len(items_xml), bytes=len(body))
    return body


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_item(row: object, base: str) -> str:
    """Render a single ``<item>`` element for one pinned screenshot row.

    ``row`` is typed as :class:`object` because :mod:`aiosqlite`'s
    ``Row`` class isn't exported in a way mypy-strict can latch onto;
    we coerce every column explicitly through ``str`` / ``int`` below
    so the runtime contract is the same regardless of the static type.
    """
    sid = int(row["id"])  # type: ignore[index]
    pinned_at_raw = str(row["pinned_at"])  # type: ignore[index]
    app_name = str(row["app_name"] or "Unknown app")  # type: ignore[index]
    window_title = str(row["window_title"] or "")  # type: ignore[index]
    alt_text_raw = row["alt_text"]  # type: ignore[index]
    ocr_text_raw = row["ocr_text"]  # type: ignore[index]

    pinned_dt = _parse_iso_utc(pinned_at_raw)
    pinned_label = pinned_dt.strftime("%Y-%m-%d %H:%M")

    title = f"Pinned at {pinned_label} ({app_name} — {window_title})"

    # Spec: alt_text when present, else first 300 chars of ocr_text.
    # Both columns are nullable; treat the empty string the same as
    # NULL so a row with ``alt_text = ''`` still falls back to OCR.
    description = ""
    if alt_text_raw is not None and str(alt_text_raw).strip():
        description = str(alt_text_raw).strip()
    elif ocr_text_raw is not None and str(ocr_text_raw).strip():
        description = str(ocr_text_raw).strip()[:_OCR_SNIPPET_LEN]

    link_url = f"{base}/screenshot/{sid}"
    guid_value = f"persona-pinned-{sid}"
    pub_date = format_datetime(pinned_dt)

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
    """Parse a ``pinned_at`` ISO-8601 string, attaching UTC if naive.

    :mod:`app.auto_pin_engine` writes ``pinned_at`` as
    ``datetime.now(UTC).isoformat(timespec="seconds")`` — always
    UTC-aware. A row written by an older path may be naive; we attach
    UTC explicitly so :func:`email.utils.format_datetime` emits a
    valid RFC-822 ``pubDate`` either way. A truly malformed value
    falls back to *now* so the feed still validates rather than
    raising mid-render.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.warning("pinned_feed.bad_ts", ts=value)
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


__all__ = ["build_pinned_rss"]
