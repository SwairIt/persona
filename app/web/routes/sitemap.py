"""RFC-compliant XML sitemap for every public-facing surface (v0.59).

What this file does
-------------------
Search engines and link-preview crawlers discover content via
``/sitemap.xml``. Persona has several opt-in public surfaces that
accumulated over the v0.44 → v0.59 stretch:

* ``/public/day/{slug}``       — admin-published days  (v0.44, table ``public_day``)
* ``/collections/queries/{slug}`` — saved-search bundles (v0.58, table ``query_collection``)
* ``/share/collection/{token}`` — multi-shot share links (v0.14, table ``share_collections``)
* ``/`` and ``/features``       — always-on main pages

Each surface gets one ``<url>`` element with the canonical ``<loc>``,
the freshest ``<lastmod>`` we can derive, and a ``<changefreq>`` hint.

Production contract
-------------------
* **RFC 0.9 compliance.** Built with :mod:`xml.etree.ElementTree` so the
  output is well-formed by construction — we never hand-concatenate
  user-supplied text into the body.
* **Parametrised SQL only.** Slugs and tokens go through the storage
  layer's ``?`` placeholders even though the columns are CHECK-validated
  upstream; the rule here is "no f-strings near a cursor".
* **Expired share collections are skipped.** A sitemap entry for a link
  that 403s on click is worse than not advertising it — search-engine
  quality signals punish dead URLs. We filter by ``expires_unix`` at
  query time so the WHERE clause hits the existing
  ``idx_share_collections_expires`` index.
* **W3C-Datetime ``<lastmod>``.** sitemaps.org accepts both date-only
  (``YYYY-MM-DD``) and full ISO-8601; we use whichever fits the source
  row to minimise lossy reformatting. SQLite's ``datetime('now')`` yields
  ``'YYYY-MM-DD HH:MM:SS'`` (UTC, no zone marker) — we parse with that
  exact shape and emit ``YYYY-MM-DDTHH:MM:SSZ`` for crawler clarity.
* **Capped row counts.** A runaway operator could in theory publish
  thousands of public days; each section is bounded so a single bad
  table doesn't blow the response past the 50k-URL / 50 MB sitemap
  ceiling. We log the cap so the operator can split into a sitemap
  index later if it ever bites.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Final
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app import blog
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.sitemap")

router = APIRouter(tags=["sitemap"])

# Sitemap protocol XML namespace. Required on the root ``<urlset>`` or
# crawlers reject the file with "no namespace" before reading any URL.
_SITEMAP_NS: Final[str] = "http://www.sitemaps.org/schemas/sitemap/0.9"

# Per-section row caps. sitemaps.org allows 50,000 URLs per sitemap;
# we stop well below that per section so the response stays cheap to
# generate and any single misconfigured table can't crowd out the others.
_MAX_PUBLIC_DAYS: Final[int] = 10_000
_MAX_QUERY_COLLECTIONS: Final[int] = 10_000
_MAX_SHARE_COLLECTIONS: Final[int] = 10_000

# SQLite's ``datetime('now')`` produces this exact shape — UTC, no zone
# marker. Mirrors the parser in :mod:`app.web.routes.audit_rss`.
_SQLITE_TS_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

# ``<changefreq>`` hints. These are advisory only — crawlers treat them
# as a soft schedule, not a contract. Values mirror how often each
# surface actually changes in practice.
_CHANGEFREQ_MAIN: Final[str] = "weekly"
_CHANGEFREQ_PUBLIC_DAY: Final[str] = "monthly"
_CHANGEFREQ_QUERY_COLLECTION: Final[str] = "weekly"
_CHANGEFREQ_SHARE_COLLECTION: Final[str] = "daily"


@router.get("/sitemap.xml")
async def sitemap_xml(request: Request) -> Response:
    """Serve ``/sitemap.xml`` listing every public-facing route.

    The response is generated synchronously per request — we expect this
    endpoint to be hit by a handful of crawlers per day, not by users,
    so caching it on disk would buy nothing and complicate freshness.
    """
    settings = get_settings()
    # Respect ``X-Forwarded-Host`` / ``X-Forwarded-Proto`` so a reverse
    # proxy can publish absolute URLs on the public hostname even though
    # uvicorn itself binds to loopback. Falls back to the loopback host
    # so a same-machine fetch still produces clickable links.
    base = _detect_base_url(request, settings.host, settings.port)
    now_iso = _format_w3c_datetime(datetime.now(UTC))

    urlset = ET.Element("urlset", {"xmlns": _SITEMAP_NS})

    # --- Always-on main pages ---------------------------------------
    _append_url(urlset, f"{base}/", now_iso, _CHANGEFREQ_MAIN)
    _append_url(urlset, f"{base}/features", now_iso, _CHANGEFREQ_MAIN)
    _append_url(urlset, f"{base}/landing", now_iso, _CHANGEFREQ_MAIN)
    _append_url(urlset, f"{base}/blog", now_iso, _CHANGEFREQ_MAIN)

    # --- Blog (file-based SEO content) ------------------------------
    blog_posts = blog.list_posts()
    for post in blog_posts:
        lastmod = f"{post.date}T00:00:00Z" if post.date else now_iso
        _append_url(urlset, f"{base}/blog/{post.slug}", lastmod, _CHANGEFREQ_PUBLIC_DAY)

    # --- Per-row sections -------------------------------------------
    public_days = await _fetch_public_days()
    for slug, lastmod in public_days:
        _append_url(
            urlset,
            f"{base}/public/day/{slug}",
            lastmod,
            _CHANGEFREQ_PUBLIC_DAY,
        )

    query_collections = await _fetch_query_collections()
    for slug, lastmod in query_collections:
        _append_url(
            urlset,
            f"{base}/collections/queries/{slug}",
            lastmod,
            _CHANGEFREQ_QUERY_COLLECTION,
        )

    share_collections = await _fetch_share_collections()
    for token, lastmod in share_collections:
        _append_url(
            urlset,
            f"{base}/share/collection/{token}",
            lastmod,
            _CHANGEFREQ_SHARE_COLLECTION,
        )

    body = ET.tostring(
        urlset,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=False,
    )

    log.info(
        "sitemap.served",
        public_days=len(public_days),
        query_collections=len(query_collections),
        share_collections=len(share_collections),
        urls_total=len(public_days)
        + len(query_collections)
        + len(share_collections)
        + 2,
    )
    return Response(content=body, media_type="text/xml; charset=utf-8")


# ---------------------------------------------------------------------------
# DB fetchers
# ---------------------------------------------------------------------------


async def _fetch_public_days() -> list[tuple[str, str]]:
    """Return ``(slug, lastmod_iso)`` for every published day.

    Ordered newest-first so if we ever hit the cap the freshest content
    is the content that gets advertised. ``published_at`` is the only
    timestamp the table stores; we treat it as the day's lastmod.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT slug, published_at "
            "FROM public_day "
            "ORDER BY published_at DESC "
            "LIMIT ?",
            (_MAX_PUBLIC_DAYS,),
        )
        rows = await cursor.fetchall()
    out: list[tuple[str, str]] = []
    for row in rows:
        slug = str(row["slug"])
        lastmod = _normalise_sqlite_ts(row["published_at"])
        out.append((slug, lastmod))
    if len(out) >= _MAX_PUBLIC_DAYS:
        log.warning("sitemap.public_day_cap_hit", cap=_MAX_PUBLIC_DAYS)
    return out


async def _fetch_query_collections() -> list[tuple[str, str]]:
    """Return ``(slug, lastmod_iso)`` for every query collection.

    ``query_collection.created_at`` is the only freshness signal on the
    table (members can change but the storage layer does not record
    when); using created_at is honest and stable across reloads.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT slug, created_at "
            "FROM query_collection "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (_MAX_QUERY_COLLECTIONS,),
        )
        rows = await cursor.fetchall()
    out: list[tuple[str, str]] = []
    for row in rows:
        slug = str(row["slug"])
        lastmod = _normalise_sqlite_ts(row["created_at"])
        out.append((slug, lastmod))
    if len(out) >= _MAX_QUERY_COLLECTIONS:
        log.warning("sitemap.query_collection_cap_hit", cap=_MAX_QUERY_COLLECTIONS)
    return out


async def _fetch_share_collections() -> list[tuple[str, str]]:
    """Return ``(token, lastmod_iso)`` for every non-expired share collection.

    Expired tokens are filtered server-side — surfacing a link that
    already 403s on click would waste crawl budget and damage quality
    signals. The ``WHERE expires_unix > ?`` clause hits
    ``idx_share_collections_expires`` so this stays cheap even with a
    long table.
    """
    now_unix = int(time.time())
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT token, created_at "
            "FROM share_collections "
            "WHERE expires_unix > ? "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (now_unix, _MAX_SHARE_COLLECTIONS),
        )
        rows = await cursor.fetchall()
    out: list[tuple[str, str]] = []
    for row in rows:
        token = str(row["token"])
        lastmod = _normalise_sqlite_ts(row["created_at"])
        out.append((token, lastmod))
    if len(out) >= _MAX_SHARE_COLLECTIONS:
        log.warning("sitemap.share_collection_cap_hit", cap=_MAX_SHARE_COLLECTIONS)
    return out


# ---------------------------------------------------------------------------
# XML + formatting helpers
# ---------------------------------------------------------------------------


def _append_url(
    parent: ET.Element,
    loc: str,
    lastmod: str,
    changefreq: str,
) -> None:
    """Append one fully-populated ``<url>`` element to ``parent``.

    ``ElementTree`` handles XML-escaping of the text content for us, so
    callers can pass raw slugs / tokens without worrying about ``&``,
    ``<``, or ``>`` in user-supplied identifiers. (Slugs and tokens are
    constrained upstream, but defensive escaping costs nothing here.)
    """
    url_el = ET.SubElement(parent, "url")
    ET.SubElement(url_el, "loc").text = loc
    ET.SubElement(url_el, "lastmod").text = lastmod
    ET.SubElement(url_el, "changefreq").text = changefreq


def _normalise_sqlite_ts(raw: object) -> str:
    """Convert a SQLite timestamp string to a W3C-Datetime ``lastmod``.

    Returns the current UTC time on parse failure so a single malformed
    row can't sink the whole sitemap — emitting an empty ``<lastmod>``
    would fail sitemap validators that some search consoles run.
    """
    if not isinstance(raw, str) or not raw:
        log.warning("sitemap.bad_ts_missing", raw=repr(raw))
        return _format_w3c_datetime(datetime.now(UTC))
    try:
        parsed = datetime.strptime(raw, _SQLITE_TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        log.warning("sitemap.bad_ts", ts=raw)
        return _format_w3c_datetime(datetime.now(UTC))
    return _format_w3c_datetime(parsed)


def _format_w3c_datetime(dt: datetime) -> str:
    """Format ``dt`` as ``YYYY-MM-DDTHH:MM:SSZ`` (W3C-Datetime, UTC).

    sitemaps.org accepts any W3C-Datetime form; we use the second-
    precision UTC form because it round-trips cleanly through every
    validator we have tested.
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _detect_base_url(request: Request, fallback_host: str, fallback_port: int) -> str:
    """Derive ``scheme://host`` for absolute ``<loc>`` URLs.

    Honours ``X-Forwarded-Proto`` and ``X-Forwarded-Host`` so a reverse
    proxy can publish the sitemap on the public hostname even though
    Persona's uvicorn process listens on loopback. Falls back to the
    bound ``host:port`` so a direct same-machine fetch still yields
    clickable links.
    """
    forwarded_host = request.headers.get("x-forwarded-host")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    host_header = request.headers.get("host")

    if forwarded_host:
        host = forwarded_host.split(",", 1)[0].strip()
    elif host_header:
        host = host_header.strip()
    else:
        host = f"{fallback_host}:{fallback_port}"

    if forwarded_proto:
        scheme = forwarded_proto.split(",", 1)[0].strip().lower()
    else:
        scheme = request.url.scheme or "http"

    return f"{scheme}://{host}"


__all__ = ["router"]
