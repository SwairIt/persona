"""Single discovery page that lists every RSS feed Persona exposes.

Why this exists
---------------
Persona's RSS surface has grown organically: ``/feeds/journal.rss`` ships
with the first release, ``/feeds/audit.rss`` lands in v0.47, per-tag and
per-auto-collection feeds arrive over later ticks, and every saved
search now has its own ``/feeds/saved-search/{id}.rss``. A new user (or
a returning one looking for *that* feed they bookmarked once) has no
single place to discover the lot.

``GET /rss`` answers that question. It is **purely a UI page** — it
performs read-only catalog queries against the same tables the
individual feed handlers consume and renders the resulting URLs as
copy-pasteable rows. Nothing here writes; nothing here serves XML.

Design contract
---------------
* **Always-available section first.** ``/feeds/journal.rss`` and
  ``/feeds/audit.rss`` (loopback-only) are listed unconditionally so
  the page is useful even on a fresh install with no tags or saved
  searches.
* **Top 20 tags only.** A long-running instance can accumulate
  thousands of tags; rendering them all turns this page into a wall of
  text. We sort by usage count and cap at 20, mirroring the "useful
  defaults" pattern in :mod:`app.web.routes.tags`.
* **All auto_collections.** These are operator-curated and capped to
  ~40 in practice; listing every row matches the spec.
* **All saved searches.** Same rationale — operator-curated.
* **No private filtering.** This is a discovery page, not the feed
  itself; each linked endpoint enforces its own gate (e.g. the audit
  feed and private collections both 403 on non-loopback). Surfacing
  the URL is harmless.
* **Copy buttons inline.** The whole point of the page is "give me the
  URL to paste into my reader" — a copy button per row is the primary
  affordance.

Routes that this page links to:

* ``/feeds/journal.rss`` — :func:`app.web.routes.rss.journal_rss`
* ``/feeds/audit.rss`` — :func:`app.web.routes.audit_rss.audit_rss`
* ``/feeds/tags/{name}.rss`` — :func:`app.web.routes.rss.tag_rss`
* ``/feeds/collection/{slug}.rss`` — :func:`app.web.routes.rss.collection_rss`
* ``/feeds/saved-search/{id}.rss`` — :func:`app.web.routes.rss.saved_search_rss`
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from collections.abc import Sequence

router = APIRouter(tags=["feeds"])
log = get_logger("persona.rss_index")

# Cap mirrors the spec: a discovery page should surface the *useful*
# slice of a long tail, not the entire long tail. The tag table can
# trivially exceed a few thousand rows on a multi-month install.
_TOP_TAGS_LIMIT = 20


class FeedRow(TypedDict):
    """One displayable feed entry.

    Kept as a ``TypedDict`` rather than a dataclass so it round-trips
    cleanly into the Jinja context with attribute *and* item access.
    """

    title: str
    url: str
    description: str


class FeedSection(TypedDict):
    """One labelled group of feeds (e.g. "Per-tag feeds")."""

    slug: str
    label: str
    blurb: str
    rows: list[FeedRow]


async def _load_top_tags(limit: int) -> list[FeedRow]:
    """Return the ``limit`` most-used tags as displayable feed rows.

    The query mirrors :func:`app.storage.tags.list_tags` (which orders
    by ``COUNT(st.screenshot_id) DESC`` already) but pulls only the
    columns the index page needs — no colour, no full count list — and
    caps with ``LIMIT`` so a multi-thousand-tag install still renders
    in a single page-paint.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT t.name AS name, COUNT(st.screenshot_id) AS n "
            "FROM tags t LEFT JOIN screenshot_tags st ON st.tag_id = t.id "
            "GROUP BY t.id, t.name "
            "HAVING n > 0 "
            "ORDER BY n DESC, t.name ASC "
            "LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    return [
        FeedRow(
            title=f"#{row['name']}",
            url=f"/feeds/tags/{row['name']}.rss",
            description=f"{int(row['n'])} tagged shot{'s' if int(row['n']) != 1 else ''}",
        )
        for row in rows
    ]


async def _load_auto_collections() -> list[FeedRow]:
    """Return every ``auto_collection`` row as a displayable feed row.

    Both ``public = 1`` and ``public = 0`` rules are surfaced — the
    feed itself enforces the loopback gate on private rules
    (:func:`app.web.routes.rss.collection_rss`). Suppressing private
    slugs here would only hide them from the local operator (i.e. the
    one person allowed to use them).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT slug, title, tag, public "
            "FROM auto_collection "
            "ORDER BY created_at DESC, slug ASC"
        )
        rows = await cursor.fetchall()
    out: list[FeedRow] = []
    for row in rows:
        is_public = int(row["public"]) == 1
        gate_note = "" if is_public else " (loopback-only)"
        out.append(
            FeedRow(
                title=str(row["title"]),
                url=f"/feeds/collection/{row['slug']}.rss",
                description=f"Tag #{row['tag']}{gate_note}",
            )
        )
    return out


async def _load_saved_searches() -> list[FeedRow]:
    """Return every saved search as a displayable feed row.

    Persona's "per-search" RSS lives at ``/feeds/saved-search/{id}.rss``
    rather than the original spec's ``/search.rss?q=…`` — the saved-
    search table provides a stable id and human label, which is what
    feed readers want when they bookmark a subscription.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, query, app_name "
            "FROM saved_searches "
            "ORDER BY created_at DESC, id DESC"
        )
        rows = await cursor.fetchall()
    out: list[FeedRow] = []
    for row in rows:
        scope = str(row["app_name"]) if row["app_name"] else "all apps"
        out.append(
            FeedRow(
                title=str(row["name"]),
                url=f"/feeds/saved-search/{int(row['id'])}.rss",
                description=f"Query: {row['query']!s} · Scope: {scope}",
            )
        )
    return out


def _static_section_journal() -> FeedSection:
    """The always-on journal feed.

    Broken out as a helper so the route body reads as a flat list of
    sections rather than a 30-line dict literal.
    """
    return FeedSection(
        slug="journal",
        label="Journal",
        blurb=(
            "The newest 200 screenshot notes — your hand-written annotations, "
            "freshest first."
        ),
        rows=[
            FeedRow(
                title="Journal",
                url="/feeds/journal.rss",
                description="Most-recent screenshot notes (up to 200).",
            ),
        ],
    )


def _static_section_audit() -> FeedSection:
    """The loopback-only audit feed."""
    return FeedSection(
        slug="audit",
        label="Audit log",
        blurb=(
            "Privileged admin actions on this instance. Loopback-only — only "
            "feed readers running on this machine can poll it."
        ),
        rows=[
            FeedRow(
                title="Audit log",
                url="/feeds/audit.rss",
                description="Last 100 privileged actions (loopback-only).",
            ),
        ],
    )


def _build_sections(
    top_tags: Sequence[FeedRow],
    collections: Sequence[FeedRow],
    saved_searches: Sequence[FeedRow],
) -> list[FeedSection]:
    """Assemble the section list in the order the template renders.

    Static sections come first so the page is useful on a fresh install;
    dynamic sections follow and degrade to a friendly empty-state in the
    template when their row list is empty.
    """
    return [
        _static_section_journal(),
        _static_section_audit(),
        FeedSection(
            slug="tags",
            label=f"Per-tag feeds (top {_TOP_TAGS_LIMIT})",
            blurb=(
                "One feed per tag — the 50 most-recent shots carrying that "
                "tag, OCR snippets passed through your redaction rules."
            ),
            rows=list(top_tags),
        ),
        FeedSection(
            slug="collections",
            label="Auto-collection feeds",
            blurb=(
                "One feed per auto-collection rule. Private rules are "
                "surfaced here for your own discovery; the feed itself is "
                "still loopback-gated."
            ),
            rows=list(collections),
        ),
        FeedSection(
            slug="saved-searches",
            label="Saved-search feeds",
            blurb=(
                "One feed per saved search — subscribe to a query and get a "
                "ping every time a new shot matches."
            ),
            rows=list(saved_searches),
        ),
    ]


@router.get("/rss", response_class=HTMLResponse)
async def rss_index_page(request: Request) -> HTMLResponse:
    """Render the single discovery page that lists every RSS feed.

    Read-only: three small catalog queries against ``tags``,
    ``auto_collection`` and ``saved_searches``. No XML is emitted from
    this handler — the actual feeds live in :mod:`app.web.routes.rss`
    and :mod:`app.web.routes.audit_rss`.
    """
    top_tags = await _load_top_tags(_TOP_TAGS_LIMIT)
    collections = await _load_auto_collections()
    saved_searches = await _load_saved_searches()
    sections = _build_sections(top_tags, collections, saved_searches)

    total_dynamic = len(top_tags) + len(collections) + len(saved_searches)
    log.info(
        "rss_index.rendered",
        tags=len(top_tags),
        collections=len(collections),
        saved_searches=len(saved_searches),
        total_dynamic=total_dynamic,
    )

    return templates.TemplateResponse(
        request,
        "rss_index.html",
        {
            "title": "RSS feeds",
            "active_nav": "settings",
            "sections": sections,
            "total_dynamic": total_dynamic,
        },
    )


__all__ = ["router"]
