"""RSS / Atom feed for journal entries — so you can subscribe to your own past."""

from __future__ import annotations

import html as html_mod
from datetime import UTC, datetime, timezone
from email.utils import format_datetime
from ipaddress import ip_address
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.storage.time import parse_iso

router = APIRouter(prefix="/feeds", tags=["feeds"])

# Mirror of auto_collections._MAX_SHOTS_PER_COLLECTION but capped tighter:
# RSS readers don't need 500 items per poll, 50 is the spec.
_MAX_RSS_ITEMS = 50
_OCR_SNIPPET_LEN = 240

_collection_log = get_logger("persona.rss.collection")


@router.get("/journal.rss")
async def journal_rss(request: Request) -> Response:
    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT n.screenshot_id, n.body, n.updated_at,
                   s.captured_at, s.app_name, s.window_title
            FROM screenshot_notes n
            JOIN screenshots s ON s.id = n.screenshot_id
            ORDER BY n.updated_at DESC
            LIMIT 200
            """
        )
        rows = await cursor.fetchall()

    items_xml = []
    for row in rows:
        sid = int(row["screenshot_id"])
        title = (row["app_name"] or "Untitled") + " — " + (row["window_title"] or "")
        body_text = str(row["body"]).strip()
        link = f"{base}/screenshot/{sid}"
        pub = parse_iso(str(row["updated_at"]))
        items_xml.append(_rss_item(title, body_text, link, pub, sid))

    last_build = format_datetime(datetime.now(timezone.utc))
    joined_items = "\n".join(items_xml)
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Persona Journal</title>
    <link>{base}/journal</link>
    <atom:link href="{base}/feeds/journal.rss" rel="self" type="application/rss+xml" />
    <description>Notes from my Persona memory — most-recent updates first.</description>
    <lastBuildDate>{last_build}</lastBuildDate>
{joined_items}
  </channel>
</rss>
"""
    return Response(content=body, media_type="application/rss+xml; charset=utf-8")


def _is_loopback_client(request: Request) -> bool:
    """True when the request originates from the same host.

    Mirrors ``auto_collections._is_loopback_client`` rather than importing it,
    so the RSS module stays self-contained and the routes module has no
    circular-import risk via the templates engine.
    """
    client = request.client
    if client is None:
        return False
    try:
        return ip_address(client.host).is_loopback
    except ValueError:
        return False


def _rss_item(title: str, body: str, link: str, pub: datetime, sid: int) -> str:
    guid_url = f"{link}#note-{sid}"
    return f"""    <item>
      <title>{xml_escape(title.strip(' —'))}</title>
      <link>{xml_escape(link)}</link>
      <guid isPermaLink="false">{xml_escape(guid_url)}</guid>
      <pubDate>{format_datetime(pub)}</pubDate>
      <description><![CDATA[{html_mod.escape(body)}]]></description>
    </item>"""


@router.get("/saved-search/{search_id}.rss")
async def saved_search_rss(search_id: int) -> Response:
    """RSS feed for one saved search — subscribe in any reader."""
    from app.search import search as run_search

    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT name, query, app_name FROM saved_searches WHERE id = ?",
            (search_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return Response(content="Not found", status_code=404)
        name = str(row["name"])
        query = str(row["query"])
        app_name = row["app_name"]
        hits = await run_search(conn, query=query, limit=100, app_name=app_name)

    items_xml = []
    for hit in hits:
        title = (hit.app_name or "Untitled") + " — " + (hit.window_title or "")
        snippet = (hit.snippet or "").replace("<mark>", "").replace("</mark>", "")
        link = f"{base}/screenshot/{hit.screenshot_id}"
        items_xml.append(_rss_item(title, snippet, link, hit.captured_at, hit.screenshot_id))

    last_build = format_datetime(datetime.now(timezone.utc))
    joined_items = "\n".join(items_xml)
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{xml_escape(f"Persona search: {name}")}</title>
    <link>{xml_escape(f"{base}/search?q={query}")}</link>
    <atom:link href="{base}/feeds/saved-search/{search_id}.rss" rel="self" type="application/rss+xml" />
    <description>{xml_escape(f"Hits for saved search {name!r} — query: {query}")}</description>
    <lastBuildDate>{last_build}</lastBuildDate>
{joined_items}
  </channel>
</rss>
"""
    return Response(content=body, media_type="application/rss+xml; charset=utf-8")


@router.get("/collection/{slug}.rss")
async def collection_rss(request: Request, slug: str) -> Response:
    """RSS feed for one auto-collection — subscribe to a live tag query.

    Private rules (``public = 0``) are gated to loopback, matching the gate
    on the HTML view in :mod:`app.web.routes.auto_collections`.
    """
    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT slug, title, tag, public FROM auto_collection WHERE slug = ?",
            (slug,),
        )
        row = await cursor.fetchone()
        if row is None:
            _collection_log.info("collection_rss_not_found", slug=slug)
            raise HTTPException(status_code=404, detail="Collection not found")
        rule_slug = str(row["slug"])
        rule_title = str(row["title"])
        rule_tag = str(row["tag"])
        rule_public = int(row["public"])

        if rule_public == 0 and not _is_loopback_client(request):
            _collection_log.info(
                "collection_rss_forbidden",
                slug=rule_slug,
                client=request.client.host if request.client else None,
            )
            raise HTTPException(
                status_code=403,
                detail="Private collection — local access only",
            )

        # Latest tagged shots first; LIMIT here, not after fetch, so a popular
        # tag does not balloon the join. ``screenshot_tags`` (plural) is the
        # canonical join-table name in migrations/001_tags.sql.
        cursor = await conn.execute(
            "SELECT st.screenshot_id AS sid "
            "FROM screenshot_tags st "
            "JOIN tags t ON t.id = st.tag_id "
            "JOIN screenshots s ON s.id = st.screenshot_id "
            "WHERE LOWER(t.name) = LOWER(?) "
            "ORDER BY s.captured_at DESC "
            "LIMIT ?",
            (rule_tag, _MAX_RSS_ITEMS),
        )
        id_rows = await cursor.fetchall()

        items_xml: list[str] = []
        for id_row in id_rows:
            shot = await get_screenshot(conn, int(id_row["sid"]))
            if shot is None:
                continue
            captured_str = shot.captured_at.isoformat(sep=" ", timespec="seconds")
            title = f"{shot.app_name or 'Untitled'} — {captured_str}"
            link = f"{base}/screenshot/{shot.id}"
            snippet = (shot.ocr_text or "").strip()[:_OCR_SNIPPET_LEN]
            items_xml.append(_rss_item(title, snippet, link, shot.captured_at, shot.id))

    _collection_log.info(
        "collection_rss_served",
        slug=rule_slug,
        tag=rule_tag,
        items=len(items_xml),
    )

    # ``datetime.UTC`` is the py3.11+ canonical alias for ``timezone.utc``;
    # the sibling routes still use the legacy form but new code matches lint.
    last_build = format_datetime(datetime.now(UTC))
    joined_items = "\n".join(items_xml)
    feed_title = f"Persona — {rule_title}"
    feed_desc = f"Auto-collection for tag #{rule_tag}"
    self_link = xml_escape(f"{base}/feeds/collection/{rule_slug}.rss")
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{xml_escape(feed_title)}</title>
    <link>{xml_escape(f"{base}/collection/{rule_slug}")}</link>
    <atom:link href="{self_link}" rel="self" type="application/rss+xml" />
    <description>{xml_escape(feed_desc)}</description>
    <lastBuildDate>{last_build}</lastBuildDate>
{joined_items}
  </channel>
</rss>
"""
    return Response(content=body, media_type="application/rss+xml; charset=utf-8")
