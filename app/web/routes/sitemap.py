"""RFC-compliant XML sitemap for every public-facing surface (v0.59).

Sitemap index (2026-08-25)
--------------------------
``/sitemap.xml`` is now a **sitemap index** pointing at child maps served by
``/sitemap-{slug}.xml``:

* ``/sitemap-pages.xml``      — the always-on marketing/legal pages
* ``/sitemap-posts.xml``      — every blog article
* ``/sitemap-categories.xml`` — ``/blog/category/{slug}`` hub pages
* ``/sitemap-tags.xml``       — ``/blog/tag/{slug}`` hub pages
* ``/sitemap-public.xml``     — published days, query collections, share links

The 350-article corpus still fits in one file (the protocol allows 50,000
URLs), so this split buys nothing *today*. It is done now because the
alternative is doing it later: once a sitemap URL has been fetched and
remembered by a search engine, restructuring it costs re-discovery of every
URL underneath. The children are ONE parameterised route rather than five
literal ones — ``REGISTERED_ROUTE_BUDGET`` has no headroom and five
near-identical handlers would have been five copies of the same six lines.

What this file does
-------------------
Search engines and link-preview crawlers discover content via
``/sitemap.xml``. Persona has several opt-in public surfaces that
accumulated over the v0.44 → v0.59 stretch:

* ``/public/day/{slug}``       — admin-published days  (v0.44, table ``public_day``)
* ``/collections/queries/{slug}`` — saved-search bundles (v0.58, table ``query_collection``)
* ``/share/collection/{token}`` — multi-shot share links (v0.14, table ``share_collections``)
* ``/``, ``/features``, ``/pricing``, ``/security`` and the legal block
  (``/privacy-policy*``, ``/terms*``) — always-on main pages, listed in
  :data:`_STATIC_PUBLIC_PATHS`

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

# Every always-on public page, in one list so "is it crawlable?" has a single
# answer. Membership here must match ``_PUBLIC_PREFIXES`` in
# :mod:`app.web.middleware.auth_gate` — a path the gate keeps behind a session
# would be advertised to crawlers as a 303 to /landing.
#
# The legal + commercial block (``/pricing`` and everything under
# ``/privacy-policy`` and ``/terms``, plus ``/security``) was missing entirely.
# Those are exactly the pages a marketplace or payment provider checks for, and
# an unlisted page is a page they may not find.
_STATIC_PUBLIC_PATHS: Final[tuple[str, ...]] = (
    "/",
    "/features",
    "/landing",
    "/blog",
    # Server-rendered blog search. Listed because the empty-query page is a
    # real, useful landing page ("поиск по блогу"); ``robots.txt`` separately
    # disallows ``/blog/search?…`` so the infinite result-page space is not
    # crawled. The feeds (/blog/rss.xml, /blog/atom.xml) are deliberately NOT
    # here: a feed is discovered through <link rel="alternate">, and listing
    # one in a urlset asks a crawler to index the XML as a page.
    "/blog/search",
    # ``/compare`` REMOVED (2026-08-25). It was listed here as if it were a
    # marketing comparison page. It is not: the only ``GET /compare`` in the
    # app is the owner's screenshot side-by-side diff
    # (``app/web/routes/shot_compare.py``), which requires a session AND two
    # screenshot ids, and answers 303 to an anonymous crawler. This file's own
    # contract says a sitemap entry that redirects on click is worse than no
    # entry at all — caught by
    # ``tests/test_blog_seo.py::test_every_advertised_url_answers_for_an_anonymous_visitor``.
    #
    # NOTE for whoever owns the auth gate: ``/compare`` is ALSO still in
    # ``_PUBLIC_PREFIXES`` (app/web/middleware/auth_gate.py). Nothing under
    # that prefix is meant to be public; today only the route's own
    # ``current_user_required`` dependency keeps it closed. That line should
    # go too, but it is outside this change's blast radius.
    "/pricing",
    "/roadmap",
    "/changelog",
    "/security",
    "/privacy-policy",
    "/privacy-policy/consent",
    "/privacy-policy/cookies",
    "/terms",
    "/terms/offer",
    "/terms/refund",
    "/terms/requisites",
)


#: Namespace for the ``<sitemapindex>`` document. Same URI as ``urlset`` —
#: the protocol reuses it for both root elements.
_SITEMAP_INDEX_NS: Final[str] = _SITEMAP_NS

#: Child maps, in the order the index lists them. Adding a section here is
#: the ONLY place that needs to change: the index and the child handler both
#: read this tuple, so they cannot disagree about which sections exist.
SITEMAP_SECTIONS: Final[tuple[str, ...]] = (
    "pages",
    "posts",
    "categories",
    "tags",
    "public",
)

#: A tag page is only worth advertising once it groups more than one article.
#: See the ``tags`` branch of :func:`sitemap_section_xml`.
_MIN_TAG_POSTS_FOR_SITEMAP: Final[int] = 2


@router.get("/sitemap.xml")
async def sitemap_xml(request: Request) -> Response:
    """Serve ``/sitemap.xml`` as a sitemap **index** over the child maps.

    Generated synchronously per request — a handful of crawler fetches per
    day, so caching it on disk would buy nothing and complicate freshness.
    """
    settings = get_settings()
    # Respect ``X-Forwarded-Host`` / ``X-Forwarded-Proto`` so a reverse
    # proxy can publish absolute URLs on the public hostname even though
    # uvicorn itself binds to loopback. Falls back to the loopback host
    # so a same-machine fetch still produces clickable links.
    base = _detect_base_url(request, settings.host, settings.port)
    now_iso = _format_w3c_datetime(datetime.now(UTC))

    index = ET.Element("sitemapindex", {"xmlns": _SITEMAP_INDEX_NS})
    for section in SITEMAP_SECTIONS:
        entry = ET.SubElement(index, "sitemap")
        ET.SubElement(entry, "loc").text = f"{base}/sitemap-{section}.xml"
        ET.SubElement(entry, "lastmod").text = now_iso

    body = ET.tostring(
        index, encoding="utf-8", xml_declaration=True, short_empty_elements=False
    )
    log.info("sitemap.index_served", sections=len(SITEMAP_SECTIONS))
    return Response(content=body, media_type="text/xml; charset=utf-8")


@router.get("/sitemap-{slug}.xml")
async def sitemap_section_xml(request: Request, slug: str) -> Response:
    """Serve one child sitemap. Unknown section → 404, never an empty urlset.

    An empty ``<urlset>`` for ``/sitemap-typo.xml`` would answer 200 and tell
    a crawler "this section exists and is empty", which is exactly the signal
    we do not want to send about a URL that is simply wrong.
    """
    if slug not in SITEMAP_SECTIONS:
        return Response(
            content=f"unknown sitemap section: {slug}"[:200],
            status_code=404,
            media_type="text/plain; charset=utf-8",
        )
    settings = get_settings()
    base = _detect_base_url(request, settings.host, settings.port)
    now_iso = _format_w3c_datetime(datetime.now(UTC))
    urlset = ET.Element("urlset", {"xmlns": _SITEMAP_NS})
    count = 0

    if slug == "pages":
        for path in _STATIC_PUBLIC_PATHS:
            _append_url(urlset, f"{base}{path}", now_iso, _CHANGEFREQ_MAIN)
        count = len(_STATIC_PUBLIC_PATHS)

    elif slug == "posts":
        # ``blog.list_posts`` already drops ``noindex`` articles — advertising
        # a page we ask not to be indexed is a contradiction a crawler is
        # entitled to hold against the whole domain.
        posts = blog.list_posts()
        for post in posts:
            stamp = post.last_modified
            lastmod = f"{stamp}T00:00:00Z" if stamp else now_iso
            _append_url(urlset, f"{base}{post.path}", lastmod, _CHANGEFREQ_PUBLIC_DAY)
        count = len(posts)

    elif slug == "categories":
        taxons = blog.categories()
        for taxon in taxons:
            _append_url(
                urlset,
                f"{base}/blog/category/{taxon.slug}",
                now_iso,
                _CHANGEFREQ_MAIN,
            )
        count = len(taxons)

    elif slug == "tags":
        # Only tags that actually group something get advertised. A tag page
        # listing ONE article is, to a crawler, a near-duplicate of that
        # article with no added content — and at 350 posts with 3-6 tags each
        # there are hundreds of them. The pages still answer 200 (they are
        # linked from the articles that carry the tag); we just do not ask a
        # search engine to spend crawl budget discovering them.
        taxons = [t for t in blog.tags() if t.count >= _MIN_TAG_POSTS_FOR_SITEMAP]
        for taxon in taxons:
            _append_url(
                urlset, f"{base}/blog/tag/{taxon.slug}", now_iso, _CHANGEFREQ_MAIN
            )
        count = len(taxons)

    else:  # "public" — the DB-backed opt-in surfaces
        public_days = await _fetch_public_days()
        for day_slug, lastmod in public_days:
            _append_url(
                urlset,
                f"{base}/public/day/{day_slug}",
                lastmod,
                _CHANGEFREQ_PUBLIC_DAY,
            )
        query_collections = await _fetch_query_collections()
        for coll_slug, lastmod in query_collections:
            _append_url(
                urlset,
                f"{base}/collections/queries/{coll_slug}",
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
        count = (
            len(public_days) + len(query_collections) + len(share_collections)
        )

    body = ET.tostring(
        urlset, encoding="utf-8", xml_declaration=True, short_empty_elements=False
    )
    log.info("sitemap.section_served", section=slug, urls=count)
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

    The logic itself lives in :func:`app.blog.resolve_base_url` — the blog
    canonicals, the JSON-LD absolute URLs, the RSS/Atom ``<link>`` elements
    and this sitemap must all agree on what this site's origin is, and three
    copies of a header-precedence rule is how they stop agreeing. This
    function stays as the name the sitemap code and its callers already use.
    """
    return blog.resolve_base_url(
        request.headers,
        request_scheme=request.url.scheme,
        fallback_host=fallback_host,
        fallback_port=fallback_port,
    )


__all__ = ["router"]
