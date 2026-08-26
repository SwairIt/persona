"""Public blog + SEO endpoints.

Routes (all public — under the ``/blog`` prefix in the auth-gate allow-list):
    * GET /blog                    — article index, paginated (24/page)
    * GET /blog/search?q=          — server-rendered search (``&format=json``
                                     returns the same ranking as JSON)
    * GET /blog/rss.xml            — RSS 2.0 feed
    * GET /blog/atom.xml           — Atom 1.0 feed
    * GET /blog/category/{slug}    — crawlable category page, paginated
    * GET /blog/tag/{slug}         — crawlable tag page, paginated
    * GET /blog/{slug}             — single article (sticky TOC, JSON-LD)
    * GET /robots.txt              — allow all + sitemap pointer

Registration order matters. Starlette matches in order, so every literal
(``/blog/search``, ``/blog/rss.xml``, ``/blog/atom.xml``) is declared BEFORE
the ``/blog/{slug}`` pattern that would otherwise swallow it —
``tests/test_full_site_smoke.py::test_no_literal_route_is_shadowed_by_an_earlier_pattern``
exists because exactly this went wrong three times elsewhere in the app.

Why category and tag pages exist at all: the category filter used to be
client-side only, so for a crawler those pages did not exist. 350 articles
across 8 categories and ~120 tags with no indexable hub page is 350 orphans.

Blog content is file-based (see app/blog.py) — global site content, not
per-user data, so nothing here is user-scoped and this module touches no
database (it must stay out of ``tests/architecture_route_db_debt.txt``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from jinja2 import TemplateNotFound

from app import blog
from app.auth import current_user_optional
from app.auth.sessions import SessionRecord
from app.settings import get_settings
from app.web.templates_engine import templates


async def _warm_corpus_once() -> None:
    """Kick the corpus + search-index warm-up on the first blog request.

    Attached to the router rather than called at import: doing it at import
    put ~2 s of file reading and tokenising inside ``app.web.main``'s import
    window and blew the cold-import budget. See
    :func:`app.blog.warm_up_in_background` for the measurement. After the
    first call this is a semaphore check that never blocks.
    """
    blog.warm_up_in_background()


router = APIRouter(tags=["blog"], dependencies=[Depends(_warm_corpus_once)])

#: Newest N posts in a feed. Readers do not want 350 entries and feed
#: validators warn above a few hundred KB.
FEED_LIMIT = 30

#: Cap on server-rendered search results. Beyond this a query is too broad to
#: be useful and the page becomes a scraping target.
SEARCH_LIMIT = 50

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _base_url(request: Request) -> str:
    """Absolute origin for canonicals / JSON-LD, honouring ``X-Forwarded-*``."""
    settings = get_settings()
    return blog.resolve_base_url(
        request.headers,
        request_scheme=request.url.scheme,
        fallback_host=settings.host,
        fallback_port=settings.port,
    )


def _render(
    request: Request,
    candidates: tuple[str, ...],
    context: dict[str, Any],
    status_code: int = 200,
) -> HTMLResponse:
    """Render the first template that exists, falling back down the list.

    The blog templates are owned by a different workstream than these routes.
    Rather than 500-ing on a page whose template has not landed yet, we fall
    back to ``blog_index.html``, which every listing context is shaped to
    satisfy. Once the dedicated template exists it simply wins.
    """
    for name in candidates:
        try:
            templates.env.get_template(name)
        except TemplateNotFound:
            continue
        return templates.TemplateResponse(
            request, name, context, status_code=status_code
        )
    return templates.TemplateResponse(
        request, "blog_index.html", context, status_code=status_code
    )


def _listing_context(
    request: Request,
    session: SessionRecord | None,
    *,
    posts: list[blog.BlogPost],
    page_number: int,
    title: str,
    heading: str,
    description: str,
    canonical_path: str,
    kind: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One context shape for every listing page (index, category, tag, search).

    Keeping the shape identical is what lets the template fallback above work
    and what stops four near-identical templates from drifting apart.
    """
    base = _base_url(request)
    page = blog.paginate(posts, page_number)
    canonical = blog.absolute(base, canonical_path)
    if page.number > 1:
        canonical = f"{canonical}?page={page.number}"

    def page_url(number: int | None) -> str | None:
        if number is None:
            return None
        return canonical_path if number == 1 else f"{canonical_path}?page={number}"

    context: dict[str, Any] = {
        "title": title,
        "heading": heading,
        "meta_description": description,
        "canonical": canonical,
        "base_url": base,
        "listing_kind": kind,
        # ``posts`` is the CURRENT PAGE — templates that ignore pagination
        # still render something correct.
        "posts": page.items,
        "page": page,
        "prev_url": page_url(page.prev_number),
        "next_url": page_url(page.next_number),
        "categories": blog.categories(),
        "category_names": blog.list_categories(),
        "tags": blog.tags(),
        "jsonld": [blog.itemlist_jsonld(page.items, base, heading)],
        "session": session,
    }
    if extra:
        context.update(extra)
    return context


def _not_found(request: Request, session: SessionRecord | None, title: str) -> HTMLResponse:
    return _render(
        request,
        ("blog_404.html",),
        {
            "title": title,
            "heading": title,
            "meta_description": title,
            "posts": [],
            "categories": blog.categories(),
            "category_names": blog.list_categories(),
            "tags": blog.tags(),
            "session": session,
        },
        status_code=404,
    )


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@router.get("/blog", response_class=HTMLResponse, response_model=None)
async def blog_index(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
) -> HTMLResponse:
    posts = blog.list_posts()
    context = _listing_context(
        request,
        session,
        posts=posts,
        page_number=page,
        title=(
            "Блог Persona — личный ИИ, память, приватность"
            if page == 1
            else f"Блог Persona — страница {page}"
        ),
        heading="Блог Persona",
        description=(
            "Статьи про личный ИИ, память и контекст, приватность, локальные "
            "модели и продуктивность. Практика и разборы без воды."
        ),
        canonical_path="/blog",
        kind="index",
        extra={"featured": [p for p in posts if p.featured][:3]},
    )
    # A page beyond the end must 404, not render an empty grid — an empty
    # ``?page=99`` that answers 200 is an infinite supply of thin pages.
    if page > context["page"].total_pages:
        return _not_found(request, session, "Такой страницы блога нет")
    return _render(request, ("blog_index.html",), context)


# ---------------------------------------------------------------------------
# Search  (literal — MUST stay above /blog/{slug})
# ---------------------------------------------------------------------------


@router.get("/blog/search", response_model=None)
async def blog_search(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
    q: str = "",
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    format: Annotated[str, Query(pattern="^(html|json)$")] = "html",
) -> Response:
    """Server-rendered blog search. Works with JavaScript off.

    ``?format=json`` returns the same ranking for the live-search box instead
    of a second endpoint — it is one query producing one ordering, and giving
    it two routes would mean two things to keep in sync for no gain (the
    route budget is also full; see tests/test_architecture_gates.py).
    """
    query = (q or "").strip()[:120]
    hits = blog.search(query, limit=SEARCH_LIMIT) if query else []

    if format == "json":
        return JSONResponse(
            {
                "query": query,
                "total": len(hits),
                "results": [
                    {
                        "slug": hit.post.slug,
                        "url": hit.post.path,
                        "title": hit.post.title,
                        "excerpt": hit.post.excerpt,
                        "snippet": hit.snippet,
                        "category": hit.post.category,
                        "category_slug": hit.post.category_slug,
                        "cover": hit.post.cover,
                        "date": hit.post.date,
                        "read_minutes": hit.post.read_minutes,
                        "score": hit.score,
                    }
                    for hit in hits[:20]
                ],
            }
        )

    context = _listing_context(
        request,
        session,
        posts=[hit.post for hit in hits],
        page_number=page,
        title=(f"Поиск: {query} — блог Persona" if query else "Поиск по блогу Persona"),
        heading=(f"Поиск: {query}" if query else "Поиск по блогу"),
        description=(
            f"Результаты поиска по блогу Persona: {query}."
            if query
            else "Поиск по статьям блога Persona: личный ИИ, память, приватность."
        ),
        canonical_path="/blog/search",
        kind="search",
        extra={"query": query, "hits": hits, "total_hits": len(hits)},
    )
    return _render(request, ("blog_search.html", "blog_index.html"), context)


# ---------------------------------------------------------------------------
# Feeds  (literal — MUST stay above /blog/{slug})
# ---------------------------------------------------------------------------


@router.get("/blog/rss.xml")
async def blog_rss(request: Request) -> Response:
    """RSS 2.0 for the newest posts. ``noindex`` posts never appear."""
    base = _base_url(request)
    posts = blog.list_posts()[:FEED_LIMIT]

    rss = ET.Element("rss", {"version": "2.0", "xmlns:atom": "http://www.w3.org/2005/Atom"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Блог Persona"
    ET.SubElement(channel, "link").text = blog.absolute(base, "/blog")
    ET.SubElement(channel, "description").text = (
        "Личный ИИ, память и контекст, приватность, локальные модели, продуктивность."
    )
    ET.SubElement(channel, "language").text = "ru"
    ET.SubElement(
        channel,
        "atom:link",
        {"href": blog.absolute(base, "/blog/rss.xml"), "rel": "self",
         "type": "application/rss+xml"},
    )
    ET.SubElement(channel, "lastBuildDate").text = _rfc822(
        posts[0].last_modified if posts else ""
    )
    for post in posts:
        item = ET.SubElement(channel, "item")
        url = blog.absolute(base, post.path)
        ET.SubElement(item, "title").text = post.title
        ET.SubElement(item, "link").text = url
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = url
        ET.SubElement(item, "description").text = post.excerpt
        ET.SubElement(item, "category").text = post.category
        ET.SubElement(item, "pubDate").text = _rfc822(post.date)
    return _xml_response(rss, "application/rss+xml")


@router.get("/blog/atom.xml")
async def blog_atom(request: Request) -> Response:
    """Atom 1.0 for the newest posts. ``noindex`` posts never appear."""
    base = _base_url(request)
    posts = blog.list_posts()[:FEED_LIMIT]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    feed = ET.Element("feed", {"xmlns": "http://www.w3.org/2005/Atom", "xml:lang": "ru"})
    ET.SubElement(feed, "title").text = "Блог Persona"
    ET.SubElement(feed, "id").text = blog.absolute(base, "/blog")
    ET.SubElement(
        feed, "link", {"href": blog.absolute(base, "/blog"), "rel": "alternate"}
    )
    ET.SubElement(
        feed,
        "link",
        {"href": blog.absolute(base, "/blog/atom.xml"), "rel": "self",
         "type": "application/atom+xml"},
    )
    ET.SubElement(feed, "updated").text = (
        _iso_datetime(posts[0].last_modified) if posts else now
    )
    author = ET.SubElement(feed, "author")
    ET.SubElement(author, "name").text = blog.ORG_NAME
    for post in posts:
        entry = ET.SubElement(feed, "entry")
        url = blog.absolute(base, post.path)
        ET.SubElement(entry, "title").text = post.title
        ET.SubElement(entry, "id").text = url
        ET.SubElement(entry, "link", {"href": url, "rel": "alternate"})
        ET.SubElement(entry, "updated").text = _iso_datetime(post.last_modified)
        if post.date:
            ET.SubElement(entry, "published").text = _iso_datetime(post.date)
        ET.SubElement(entry, "summary", {"type": "text"}).text = post.excerpt
        ET.SubElement(entry, "category", {"term": post.category})
    return _xml_response(feed, "application/atom+xml")


def _xml_response(root: ET.Element, media_type: str) -> Response:
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True,
                       short_empty_elements=False)
    return Response(content=body, media_type=f"{media_type}; charset=utf-8")


_RFC822_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_RFC822_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _rfc822(iso_date: str) -> str:
    """``YYYY-MM-DD`` → RFC-822, which is what RSS validators demand.

    Formatted by hand rather than with ``strftime('%a, %d %b %Y')`` because
    ``%a``/``%b`` are locale-dependent: on a Russian-locale host that would
    emit ``Пн, 07 июн 2026`` and every feed reader would reject the date.
    """
    parsed = _parse_iso(iso_date)
    return (
        f"{_RFC822_DAYS[parsed.weekday()]}, {parsed.day:02d} "
        f"{_RFC822_MONTHS[parsed.month - 1]} {parsed.year} 00:00:00 +0000"
    )


def _iso_datetime(iso_date: str) -> str:
    return _parse_iso(iso_date).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(iso_date: str) -> datetime:
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


@router.get("/blog/category/{slug}", response_class=HTMLResponse, response_model=None)
async def blog_category(
    request: Request,
    slug: str,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
) -> HTMLResponse:
    name, posts = blog.posts_in_category(slug)
    if not posts:
        return _not_found(request, session, "Категория не найдена")
    context = _listing_context(
        request,
        session,
        posts=posts,
        page_number=page,
        title=(
            f"{name} — статьи блога Persona"
            if page == 1
            else f"{name} — блог Persona, страница {page}"
        ),
        heading=name,
        description=(
            f"Все статьи блога Persona в категории «{name}»: {len(posts)} "
            "материалов о личном ИИ, памяти, приватности и локальных моделях."
        ),
        canonical_path=f"/blog/category/{slug}",
        kind="category",
        extra={"taxon": blog.Taxon(name=name, slug=slug, count=len(posts))},
    )
    if page > context["page"].total_pages:
        return _not_found(request, session, "Такой страницы категории нет")
    return _render(request, ("blog_category.html", "blog_index.html"), context)


@router.get("/blog/tag/{slug}", response_class=HTMLResponse, response_model=None)
async def blog_tag(
    request: Request,
    slug: str,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
) -> HTMLResponse:
    name, posts = blog.posts_with_tag(slug)
    if not posts:
        return _not_found(request, session, "Тег не найден")
    context = _listing_context(
        request,
        session,
        posts=posts,
        page_number=page,
        title=(
            f"{name} — статьи по тегу, блог Persona"
            if page == 1
            else f"{name} — тег блога Persona, страница {page}"
        ),
        heading=f"Тег: {name}",
        description=(
            f"Статьи блога Persona с тегом «{name}» — {len(posts)} материалов."
        ),
        canonical_path=f"/blog/tag/{slug}",
        kind="tag",
        extra={"taxon": blog.Taxon(name=name, slug=slug, count=len(posts))},
    )
    if page > context["page"].total_pages:
        return _not_found(request, session, "Такой страницы тега нет")
    return _render(request, ("blog_tag.html", "blog_index.html"), context)


# ---------------------------------------------------------------------------
# Article  (pattern — MUST stay last inside /blog)
# ---------------------------------------------------------------------------


@router.get("/blog/{slug}", response_class=HTMLResponse, response_model=None)
async def blog_post(
    request: Request,
    slug: str,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse:
    post = blog.get_post(slug)
    if post is None:
        return _not_found(request, session, "Статья не найдена")
    base = _base_url(request)
    prev_post, next_post = blog.neighbours(post)
    return _render(
        request,
        ("blog_post.html",),
        {
            "title": f"{post.title} — Persona",
            "meta_description": post.excerpt,
            "canonical": blog.absolute(base, post.path),
            "base_url": base,
            "post": post,
            "prev_post": prev_post,
            "next_post": next_post,
            "related": blog.related_posts(post),
            "jsonld": blog.post_jsonld(post, base),
            "faq": post.faq,
            "breadcrumbs": [
                ("Главная", "/"),
                ("Блог", "/blog"),
                (post.category, f"/blog/category/{post.category_slug}"),
                (post.title, post.path),
            ],
            "categories": blog.categories(),
            "category_names": blog.list_categories(),
            "session": session,
        },
    )


@router.get("/robots.txt")
async def robots(request: Request) -> PlainTextResponse:
    base = _base_url(request)
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        # A crawler that indexes result pages generates infinite thin URLs.
        "Disallow: /blog/search?\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return PlainTextResponse(content=body)
