"""RSS / Atom feed for journal entries — so you can subscribe to your own past."""

from __future__ import annotations

import html as html_mod
from datetime import UTC, datetime, timezone
from email.utils import format_datetime
from ipaddress import ip_address
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.feed_tokens import verify_token as verify_feed_token
from app.logging_setup import get_logger
from app.redaction import apply_redaction
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.storage.time import parse_iso

router = APIRouter(prefix="/feeds", tags=["feeds"])

_auth_log = get_logger("persona.feed_tokens")


async def _enforce_feed_token(request: Request) -> None:
    """Reject the request unless a valid ``?token=…`` covers the path.

    No-op when :attr:`Settings.feed_auth_required` is ``False`` — that's
    the legacy open-access mode, preserved so an upgrade-in-place
    doesn't break anyone's existing feed-reader subscriptions until
    they're ready to flip the switch.

    When enforcement is on the request path (e.g.
    ``/feeds/tags/cooking.rss``) is matched against the token's
    ``feed_pattern`` via :func:`fnmatch.fnmatchcase` inside
    :func:`app.feed_tokens.verify_token`. Missing token → 401; known
    but wrong-pattern / revoked → 403; both responses are tagged in
    structlog so the operator can spot probing in the access log.
    """
    settings = get_settings()
    if not settings.feed_auth_required:
        return

    raw = request.query_params.get("token", "").strip()
    if not raw:
        _auth_log.info(
            "feed_token.gate_missing",
            path=request.url.path,
        )
        raise HTTPException(status_code=401, detail="Feed token required")

    verdict = await verify_feed_token(raw, request.url.path)
    if not verdict.get("ok"):
        # ``unknown`` collapses "no row" / "hash mismatch" — both look
        # the same to the client by design so we can't be probed for
        # token existence. ``revoked`` / ``pattern_mismatch`` deserve
        # 403 because the caller *did* present a real credential, it
        # just isn't authorised for this path right now.
        reason = verdict.get("reason", "unknown")
        if reason == "unknown":
            raise HTTPException(status_code=401, detail="Invalid feed token")
        raise HTTPException(status_code=403, detail="Feed token not authorised for this path")


# Mirror of auto_collections._MAX_SHOTS_PER_COLLECTION but capped tighter:
# RSS readers don't need 500 items per poll, 50 is the spec.
_MAX_RSS_ITEMS = 50
_OCR_SNIPPET_LEN = 240

_collection_log = get_logger("persona.rss.collection")
_tag_log = get_logger("persona.rss.tag")
_weekly_log = get_logger("persona.rss.weekly")
_annotations_log = get_logger("persona.rss.annotations")

# Annotations feed caps: 50 newest scribbles, matching the spec ceiling
# used by the other shot-derived feeds in this module.
_MAX_ANNOTATION_ITEMS = 50

# Weekly-digest feed caps: 20 most-recent rows, 400-char body snippets.
# Keeping these named constants documents the spec at the call site and
# makes future tuning (e.g. raising to 25 items) a one-line change.
_MAX_WEEKLY_ITEMS = 20
_WEEKLY_SNIPPET_LEN = 400


@router.get("/journal.rss")
async def journal_rss(request: Request) -> Response:
    await _enforce_feed_token(request)
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
async def saved_search_rss(request: Request, search_id: int) -> Response:
    """RSS feed for one saved search — subscribe in any reader."""
    await _enforce_feed_token(request)

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
    await _enforce_feed_token(request)
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


@router.get("/tags/{tag_name}.rss")
async def tag_rss(request: Request, tag_name: str) -> Response:
    """RSS feed of the most-recent shots carrying ``#tag_name``.

    Mirrors :func:`collection_rss` but resolves the tag by name (rather
    than going through an ``auto_collection`` rule). Returns 404 when
    the tag has zero tagged shots so feed readers stop polling dead
    tags instead of silently subscribing to an empty channel.

    OCR snippets are passed through :func:`apply_redaction` so any
    user-configured mask (emails, tokens, …) is honoured before the
    text ever leaves the host.
    """
    await _enforce_feed_token(request)
    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"

    async with get_connection() as conn:
        # Pull the canonical tag name (preserves the case the user
        # actually used) and the ids of the latest ``_MAX_RSS_ITEMS``
        # shots in one parametrised query. ``screenshot_tags`` (plural)
        # is the canonical join-table name in migrations/001_tags.sql.
        cursor = await conn.execute(
            "SELECT t.name AS tag_name, st.screenshot_id AS sid "
            "FROM screenshot_tags st "
            "JOIN tags t ON t.id = st.tag_id "
            "JOIN screenshots s ON s.id = st.screenshot_id "
            "WHERE LOWER(t.name) = LOWER(?) "
            "ORDER BY s.captured_at DESC "
            "LIMIT ?",
            (tag_name, _MAX_RSS_ITEMS),
        )
        id_rows = list(await cursor.fetchall())

        if not id_rows:
            _tag_log.info("tag_rss_not_found", tag=tag_name)
            raise HTTPException(status_code=404, detail=f"Tag has no shots: {tag_name}")

        canonical_name = str(id_rows[0]["tag_name"])

        items_xml: list[str] = []
        for id_row in id_rows:
            shot = await get_screenshot(conn, int(id_row["sid"]))
            if shot is None:
                continue
            captured_str = shot.captured_at.isoformat(sep=" ", timespec="seconds")
            title = f"{shot.app_name or 'Untitled'} — {captured_str}"
            link = f"{base}/screenshot/{shot.id}"
            raw_text = (shot.ocr_text or "").strip()
            redacted_text, _ = await apply_redaction(raw_text)
            snippet = redacted_text[:_OCR_SNIPPET_LEN]
            items_xml.append(_rss_item(title, snippet, link, shot.captured_at, shot.id))

    _tag_log.info(
        "tag_rss_served",
        tag=canonical_name,
        items=len(items_xml),
    )

    last_build = format_datetime(datetime.now(UTC))
    joined_items = "\n".join(items_xml)
    feed_title = f"Persona — #{canonical_name}"
    feed_desc = f"Latest shots tagged #{canonical_name}"
    self_link = xml_escape(f"{base}/feeds/tags/{canonical_name}.rss")
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{xml_escape(feed_title)}</title>
    <link>{xml_escape(f"{base}/search?q=tag:{canonical_name}")}</link>
    <atom:link href="{self_link}" rel="self" type="application/rss+xml" />
    <description>{xml_escape(feed_desc)}</description>
    <lastBuildDate>{last_build}</lastBuildDate>
{joined_items}
  </channel>
</rss>
"""
    return Response(content=body, media_type="application/rss+xml; charset=utf-8")


@router.get("/digest/weekly.rss")
async def weekly_digest_rss(request: Request) -> Response:
    """RSS 2.0 feed of the most-recent weekly LLM digests.

    Surfaces the same archive served at ``/digest/weekly-archive`` so any
    feed reader can subscribe to the Monday-Sunday retrospectives. Body
    text is XML-escaped via :func:`xml.sax.saxutils.escape` rather than
    wrapped in CDATA — digests are plain prose, not HTML, and escaping
    keeps the payload audit-safe even if a future provider sneaks ``<``
    or ``&`` into the output.
    """
    await _enforce_feed_token(request)
    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT week_start, body, generated_at "
            "FROM weekly_digest "
            "ORDER BY week_start DESC "
            "LIMIT ?",
            (_MAX_WEEKLY_ITEMS,),
        )
        rows = await cursor.fetchall()

    items_xml: list[str] = []
    for row in rows:
        week_start = str(row["week_start"])
        body_text = str(row["body"]).strip()
        snippet = body_text[:_WEEKLY_SNIPPET_LEN]
        item_title = f"Persona — week of {week_start}"
        item_link = f"{base}/digest/weekly-archive/{week_start}"
        pub = parse_iso(str(row["generated_at"]))
        guid_url = f"{item_link}#digest-{week_start}"
        items_xml.append(
            f"""    <item>
      <title>{xml_escape(item_title)}</title>
      <link>{xml_escape(item_link)}</link>
      <guid isPermaLink="false">{xml_escape(guid_url)}</guid>
      <pubDate>{format_datetime(pub)}</pubDate>
      <description>{xml_escape(snippet)}</description>
    </item>"""
        )

    _weekly_log.info("weekly_rss_served", items=len(items_xml))

    last_build = format_datetime(datetime.now(UTC))
    joined_items = "\n".join(items_xml)
    self_link = xml_escape(f"{base}/feeds/digest/weekly.rss")
    channel_link = xml_escape(f"{base}/digest/weekly-archive")
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Persona — Weekly Digests</title>
    <link>{channel_link}</link>
    <atom:link href="{self_link}" rel="self" type="application/rss+xml" />
    <description>Most-recent weekly LLM retrospectives, newest first.</description>
    <lastBuildDate>{last_build}</lastBuildDate>
{joined_items}
  </channel>
</rss>
"""
    return Response(content=body, media_type="application/rss+xml; charset=utf-8")


@router.get("/annotations.rss")
async def annotations_rss(request: Request) -> Response:
    """RSS 2.0 feed of the most-recent ``screenshot_annotation`` rows.

    Annotations are the append-only "margin scribbles" tied to a shot
    (see migrations/024_annotations.sql); this feed surfaces the newest
    ``_MAX_ANNOTATION_ITEMS`` of them so a reader can follow along as
    new notes are dropped. Body text is XML-escaped via
    :func:`xml.sax.saxutils.escape` rather than wrapped in CDATA — the
    payload is plain prose, escaping keeps it audit-safe even if a
    stray ``<`` or ``&`` sneaks in from a paste.
    """
    await _enforce_feed_token(request)
    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"

    async with get_connection() as conn:
        # Parametrised LIMIT so the cap lives in one place (the constant)
        # and no integer interpolation hits the SQL string.
        cursor = await conn.execute(
            "SELECT id, screenshot_id, body, created_at "
            "FROM screenshot_annotation "
            "ORDER BY created_at DESC, id DESC "
            "LIMIT ?",
            (_MAX_ANNOTATION_ITEMS,),
        )
        rows = await cursor.fetchall()

    items_xml: list[str] = []
    for row in rows:
        ann_id = int(row["id"])
        sid = int(row["screenshot_id"])
        body_text = str(row["body"]).strip()
        created_raw = str(row["created_at"])
        pub = parse_iso(created_raw)
        item_title = f"Shot #{sid} — {created_raw}"
        item_link = f"{base}/screenshot/{sid}"
        guid_url = f"{item_link}#annotation-{ann_id}"
        items_xml.append(
            f"""    <item>
      <title>{xml_escape(item_title)}</title>
      <link>{xml_escape(item_link)}</link>
      <guid isPermaLink="false">{xml_escape(guid_url)}</guid>
      <pubDate>{format_datetime(pub)}</pubDate>
      <description>{xml_escape(body_text)}</description>
    </item>"""
        )

    _annotations_log.info("annotations_rss_served", items=len(items_xml))

    last_build = format_datetime(datetime.now(UTC))
    joined_items = "\n".join(items_xml)
    self_link = xml_escape(f"{base}/feeds/annotations.rss")
    channel_link = xml_escape(f"{base}/journal")
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Persona — Annotations</title>
    <link>{channel_link}</link>
    <atom:link href="{self_link}" rel="self" type="application/rss+xml" />
    <description>Most-recent margin scribbles across all screenshots, newest first.</description>
    <lastBuildDate>{last_build}</lastBuildDate>
{joined_items}
  </channel>
</rss>
"""
    return Response(content=body, media_type="application/rss+xml; charset=utf-8")
