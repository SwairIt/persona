"""The crawler-facing blog surface: routes, feeds, sitemap, structured data.

Companion set: ``tests/test_blog_engine.py`` covers the engine below HTTP.

Everything here runs against a **generated corpus in a temp directory**, not
against ``app/web/content/blog``: content agents add articles continuously,
so an assertion about "the newest post" or "how many categories exist" would
be flaky against the shipped files by construction.

The templates for the new listing pages are owned by another workstream. The
routes fall back to ``blog_index.html`` when a dedicated template is absent,
so these tests assert on **status, headers, XML and the data contract** and
never on markup that is not ours to pin.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import blog
from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv
from app.web.main import create_app
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import _is_public_path
from app.web.routes import setup_gate
from app.web.routes import sitemap as sitemap_routes

# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

NEWEST_SLUG = "novejshaya-statya"
OLDEST_SLUG = "staraya-statya"
HIDDEN_SLUG = "sluzhebnaya-statya"

FAQ_ARTICLE = """Вступительный абзац про память и контекст.

## Как это устроено

Разбор механики без воды.

## Шаг 1. Установить Ollama

Скачайте установщик с сайта проекта.

## Шаг 2. Скачать модель

Выполните команду ollama pull.

## Частые вопросы

### Чем эмбеддинг отличается от обычного текста?

Эмбеддинг — это смысл текста, записанный числами, чтобы их можно было сравнивать.

### Нужен ли RAG при большом контекстном окне?

Нужен. Большое окно — это вместимость одной сессии, а не постоянная память.

## Вывод

Если вам нужна память между сессиями — берите RAG.
"""


def _write(
    directory: Path,
    slug: str,
    *,
    title: str,
    category: str,
    tags: str,
    date: str,
    body: str = "Текст статьи про память.",
    extra: str = "",
) -> None:
    (directory / f"{slug}.md").write_text(
        "---\n"
        f"title: {title}\n"
        f"slug: {slug}\n"
        f"excerpt: Описание статьи {slug} без воды.\n"
        f"category: {category}\n"
        f"tags: {tags}\n"
        "keywords: память ии, локальные модели\n"
        f"date: {date}\n"
        "cover: 🧠\n"
        f"{extra}"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def corpus_dir(tmp_path_factory) -> Path:
    """One deterministic corpus: 30 filler posts + three special ones."""
    directory = tmp_path_factory.mktemp("blog-seo")
    _write(
        directory,
        NEWEST_SLUG,
        title="Новейшая статья про память ИИ",
        category="Память",
        tags="память, RAG",
        date="2026-08-25",
        body=FAQ_ARTICLE,
        extra="updated: 2026-08-26\nauthor: Ярослав\ntype: guide\nimage: /static/blog/og/x.png\n",
    )
    _write(
        directory,
        OLDEST_SLUG,
        title="Старая статья про приватность",
        category="Приватность",
        tags="приватность",
        date="2020-01-01",
    )
    _write(
        directory,
        HIDDEN_SLUG,
        title="Служебная статья, которую не индексируем",
        category="Память",
        tags="память",
        date="2026-08-24",
        extra="noindex: true\n",
    )
    # 30 filler posts in one category → guarantees more than one page of 24.
    for i in range(30):
        _write(
            directory,
            f"filler-{i:02d}",
            title=f"Наполнитель номер {i} про локальные модели",
            category="Гайды",
            tags="локальные модели, ollama",
            date=f"2026-07-{1 + i % 28:02d}",
        )
    return directory


@pytest_asyncio.fixture
async def client(corpus_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Anonymous client against an instance whose owner already registered.

    That state matters: the auth gate only activates after the first signup,
    and "a crawler can read the blog without a session" is only a meaningful
    claim while the gate is awake. On an empty database it sleeps and every
    assertion here would pass for the wrong reason.
    """
    monkeypatch.setattr(blog, "CONTENT_DIR", corpus_dir)
    monkeypatch.setattr(blog, "_cache_file", lambda: None)
    blog.reload_posts()

    await init_database()
    owner = await create_user("owner@blog-seo.test", "Zq7-frost-lantern-91")
    async with get_connection() as conn:
        await set_kv(conn, "setup_complete", "true")
        await set_kv(conn, "owner_user_id", str(owner["id"]))
        await set_kv(conn, "owner_exclusive_mode", "0")
    setup_gate._cache.mark_done()
    auth_gate._cache["value"] = True
    auth_gate._cache["checked_at"] = 0.0

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    blog.reload_posts()


def _xml(response) -> ET.Element:
    """Parse a response body as XML — a feed that does not parse is broken."""
    assert response.status_code == 200, response.text[:300]
    return ET.fromstring(response.content)


# ---------------------------------------------------------------------------
# Index + pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blog_index_renders_and_paginates(client) -> None:
    first = await client.get("/blog")
    assert first.status_code == 200
    second = await client.get("/blog?page=2")
    assert second.status_code == 200
    assert first.text != second.text, "?page=2 served page 1"


@pytest.mark.asyncio
async def test_page_beyond_the_end_is_404_not_an_empty_grid(client) -> None:
    """A 200 on ?page=99 is an infinite supply of thin pages for a crawler."""
    assert (await client.get("/blog?page=99")).status_code == 404


@pytest.mark.asyncio
async def test_page_zero_is_rejected(client) -> None:
    assert (await client.get("/blog?page=0")).status_code == 422


@pytest.mark.asyncio
async def test_noindex_post_is_reachable_but_never_listed(client) -> None:
    assert (await client.get(f"/blog/{HIDDEN_SLUG}")).status_code == 200
    listing = await client.get("/blog")
    assert HIDDEN_SLUG not in listing.text


# ---------------------------------------------------------------------------
# Taxonomy routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_category_page_exists_for_a_crawler(client) -> None:
    response = await client.get("/blog/category/pamyat")
    assert response.status_code == 200
    assert NEWEST_SLUG in response.text


@pytest.mark.asyncio
async def test_tag_page_exists_for_a_crawler(client) -> None:
    response = await client.get("/blog/tag/rag")
    assert response.status_code == 200
    assert NEWEST_SLUG in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/blog/category/net-takoy-kategorii",
        "/blog/tag/net-takogo-tega",
        "/blog/net-takoy-stati",
    ],
)
async def test_unknown_slugs_are_404(client, path: str) -> None:
    assert (await client.get(path)).status_code == 404


@pytest.mark.asyncio
async def test_category_pagination_boundaries(client) -> None:
    """30 filler posts in "Гайды" → exactly two pages of 24."""
    assert (await client.get("/blog/category/gaydy")).status_code == 200
    assert (await client.get("/blog/category/gaydy?page=2")).status_code == 200
    assert (await client.get("/blog/category/gaydy?page=3")).status_code == 404


@pytest.mark.asyncio
async def test_taxonomy_slugs_agree_between_engine_and_sitemap(client) -> None:
    """A slug the sitemap advertises must be a slug the route serves."""
    sitemap = _xml(await client.get("/sitemap-categories.xml"))
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    locs = [el.text or "" for el in sitemap.iter(f"{ns}loc")]
    assert locs
    for loc in locs:
        path = loc.split("testserver", 1)[1]
        assert (await client.get(path)).status_code == 200, f"{path} is advertised but 404s"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_works_without_javascript(client) -> None:
    response = await client.get("/blog/search", params={"q": "память"})
    assert response.status_code == 200
    assert NEWEST_SLUG in response.text


@pytest.mark.asyncio
async def test_search_json_variant(client) -> None:
    response = await client.get("/blog/search", params={"q": "память", "format": "json"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "память"
    assert payload["total"] >= 1
    first = payload["results"][0]
    assert set(first) >= {
        "slug", "url", "title", "excerpt", "snippet", "category",
        "category_slug", "cover", "date", "read_minutes", "score",
    }
    assert first["url"] == f"/blog/{first['slug']}"


@pytest.mark.asyncio
async def test_empty_search_still_renders(client) -> None:
    response = await client.get("/blog/search")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_search_route_is_not_shadowed_by_the_slug_route(client) -> None:
    """``/blog/search`` must not be served as an article named "search"."""
    assert (await client.get("/blog/search")).status_code == 200
    assert (await client.get("/blog/search", params={"format": "json"})).json()["total"] == 0


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rss_is_valid_xml_with_the_newest_post_first(client) -> None:
    response = await client.get("/blog/rss.xml")
    assert "application/rss+xml" in response.headers["content-type"]
    root = _xml(response)
    items = root.findall("./channel/item")
    assert items, "empty feed"
    assert items[0].findtext("link", "").endswith(f"/blog/{NEWEST_SLUG}")
    assert items[0].findtext("title") == "Новейшая статья про память ИИ"
    # RFC-822, locale-independent
    assert items[0].findtext("pubDate", "").startswith("Tue, 25 Aug 2026")


@pytest.mark.asyncio
async def test_atom_is_valid_xml_with_the_newest_post_first(client) -> None:
    response = await client.get("/blog/atom.xml")
    assert "application/atom+xml" in response.headers["content-type"]
    root = _xml(response)
    ns = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(f"{ns}entry")
    assert entries
    assert entries[0].findtext(f"{ns}id", "").endswith(f"/blog/{NEWEST_SLUG}")
    # ``updated`` on the newest entry follows the front matter's ``updated``,
    # not its ``date`` — that is the whole reason the field exists.
    assert entries[0].findtext(f"{ns}updated", "").startswith("2026-08-26")


@pytest.mark.asyncio
async def test_noindex_post_is_excluded_from_both_feeds(client) -> None:
    for path in ("/blog/rss.xml", "/blog/atom.xml"):
        body = (await client.get(path)).text
        assert HIDDEN_SLUG not in body, f"{path} advertises a noindex article"


@pytest.mark.asyncio
async def test_feeds_honour_forwarded_host(client) -> None:
    response = await client.get(
        "/blog/rss.xml",
        headers={"x-forwarded-host": "persona.example.ru", "x-forwarded-proto": "https"},
    )
    assert "https://persona.example.ru/blog/" in response.text


# ---------------------------------------------------------------------------
# Sitemap index
# ---------------------------------------------------------------------------

_SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


@pytest.mark.asyncio
async def test_sitemap_is_an_index_referencing_every_child(client) -> None:
    root = _xml(await client.get("/sitemap.xml"))
    assert root.tag == f"{_SM}sitemapindex"
    locs = [el.text or "" for el in root.iter(f"{_SM}loc")]
    assert len(locs) == len(sitemap_routes.SITEMAP_SECTIONS)
    for section in sitemap_routes.SITEMAP_SECTIONS:
        assert any(loc.endswith(f"/sitemap-{section}.xml") for loc in locs), section


@pytest.mark.asyncio
async def test_every_child_sitemap_is_served_and_well_formed(client) -> None:
    root = _xml(await client.get("/sitemap.xml"))
    for loc in (el.text or "" for el in root.iter(f"{_SM}loc")):
        path = loc.split("testserver", 1)[1]
        child = _xml(await client.get(path))
        assert child.tag == f"{_SM}urlset", path


@pytest.mark.asyncio
async def test_unknown_sitemap_section_is_404(client) -> None:
    assert (await client.get("/sitemap-vydumannaya.xml")).status_code == 404


@pytest.mark.asyncio
async def test_single_article_tag_pages_are_not_advertised(client) -> None:
    """A tag page holding one article is a near-duplicate of that article.

    "приватность" is carried by exactly one post in this corpus and "rag" by
    one as well; "локальные модели" is carried by all 30 fillers. The page
    still answers 200 — it is linked from the article — it is just not worth
    a crawler's budget to go find.
    """
    body = (await client.get("/sitemap-tags.xml")).text
    assert "/blog/tag/lokalnye-modeli" in body
    assert "/blog/tag/privatnost" not in body
    assert (await client.get("/blog/tag/privatnost")).status_code == 200


@pytest.mark.asyncio
async def test_noindex_post_is_excluded_from_the_sitemap(client) -> None:
    body = (await client.get("/sitemap-posts.xml")).text
    assert f"/blog/{NEWEST_SLUG}" in body
    assert HIDDEN_SLUG not in body, "the sitemap advertises a noindex article"


@pytest.mark.asyncio
async def test_sitemap_lastmod_prefers_updated_over_date(client) -> None:
    root = _xml(await client.get("/sitemap-posts.xml"))
    for url in root.iter(f"{_SM}url"):
        if (url.findtext(f"{_SM}loc") or "").endswith(f"/blog/{NEWEST_SLUG}"):
            assert (url.findtext(f"{_SM}lastmod") or "").startswith("2026-08-26")
            return
    pytest.fail("newest post missing from the sitemap")


@pytest.mark.asyncio
async def test_sitemap_honours_forwarded_host(client) -> None:
    response = await client.get(
        "/sitemap.xml",
        headers={"x-forwarded-host": "persona.example.ru", "x-forwarded-proto": "https"},
    )
    assert "https://persona.example.ru/sitemap-posts.xml" in response.text


# ---------------------------------------------------------------------------
# The gate must agree with what the sitemap advertises
# ---------------------------------------------------------------------------


def test_every_sitemap_child_url_is_public_per_the_gate() -> None:
    """A path the gate 303s to /landing must never be in a sitemap."""
    for section in sitemap_routes.SITEMAP_SECTIONS:
        assert _is_public_path(f"/sitemap-{section}.xml"), section
    for path in sitemap_routes._STATIC_PUBLIC_PATHS:
        assert path == "/" or _is_public_path(path), path
    for prefix in ("/blog/category/pamyat", "/blog/tag/rag", "/blog/search"):
        assert _is_public_path(prefix), prefix


@pytest.mark.asyncio
async def test_every_advertised_url_answers_for_an_anonymous_visitor(client) -> None:
    """End-to-end version of the check above, over the real route table.

    This is the check that found ``/compare`` sitting in
    ``_STATIC_PUBLIC_PATHS`` while the only ``GET /compare`` handler in the
    app is the owner's screenshot diff — advertised to crawlers, 303 on
    click. The static-path membership check above cannot find that class of
    bug: the gate really does let ``/compare`` through; it is the handler's
    own auth dependency that redirects.
    """
    index = _xml(await client.get("/sitemap.xml"))
    checked = 0
    for child_loc in (el.text or "" for el in index.iter(f"{_SM}loc")):
        child = _xml(await client.get(child_loc.split("testserver", 1)[1]))
        for loc in (el.text or "" for el in child.iter(f"{_SM}loc")):
            path = loc.split("testserver", 1)[1]
            response = await client.get(path, follow_redirects=False)
            assert response.status_code == 200, (
                f"{path} is in the sitemap but answers {response.status_code}"
            )
            checked += 1
    assert checked > 30, f"walked only {checked} URLs — enumeration broke"


@pytest.mark.asyncio
async def test_robots_points_at_the_sitemap_index(client) -> None:
    body = (await client.get("/robots.txt")).text
    assert "Sitemap: http://testserver/sitemap.xml" in body
    assert "Disallow: /blog/search?" in body


# ---------------------------------------------------------------------------
# Structured data
# ---------------------------------------------------------------------------


def _post(corpus_dir: Path, monkeypatch, slug: str) -> blog.BlogPost:
    monkeypatch.setattr(blog, "CONTENT_DIR", corpus_dir)
    monkeypatch.setattr(blog, "_cache_file", lambda: None)
    blog.reload_posts()
    post = blog.get_post(slug)
    assert post is not None
    return post


def test_faqpage_is_derived_from_the_frequently_asked_questions_block(
    corpus_dir, monkeypatch
) -> None:
    """The single highest-leverage structured-data addition available."""
    post = _post(corpus_dir, monkeypatch, NEWEST_SLUG)
    data = blog.faq_jsonld(post)
    assert data is not None
    assert data["@type"] == "FAQPage"
    questions = data["mainEntity"]
    assert len(questions) == 2
    assert questions[0]["@type"] == "Question"
    assert questions[0]["name"] == "Чем эмбеддинг отличается от обычного текста?"
    assert questions[0]["acceptedAnswer"]["@type"] == "Answer"
    assert "числами" in questions[0]["acceptedAnswer"]["text"]
    # The block stops at the next h2 — "Вывод" is not an answer.
    assert all(
        "берите RAG" not in q["acceptedAnswer"]["text"] for q in questions
    )
    blog.reload_posts()


def test_article_without_faq_emits_no_faqpage(corpus_dir, monkeypatch) -> None:
    """An empty FAQPage is a Search Console error, worse than no markup."""
    post = _post(corpus_dir, monkeypatch, OLDEST_SLUG)
    assert blog.faq_jsonld(post) is None
    assert all(block["@type"] != "FAQPage" for block in blog.post_jsonld(post, "https://x"))
    blog.reload_posts()


def test_article_jsonld_shape(corpus_dir, monkeypatch) -> None:
    post = _post(corpus_dir, monkeypatch, NEWEST_SLUG)
    data = blog.article_jsonld(post, "https://persona.example.ru")
    assert data["@type"] == "TechArticle"  # type: guide
    assert data["url"] == f"https://persona.example.ru/blog/{NEWEST_SLUG}"
    assert data["datePublished"] == "2026-08-25"
    assert data["dateModified"] == "2026-08-26"
    assert data["wordCount"] == post.word_count > 0
    assert data["image"] == ["https://persona.example.ru/static/blog/og/x.png"]
    assert data["author"] == {"@type": "Person", "name": "Ярослав"}
    assert data["articleSection"] == "Память"
    blog.reload_posts()


def test_breadcrumbs_walk_home_blog_category_article(corpus_dir, monkeypatch) -> None:
    post = _post(corpus_dir, monkeypatch, NEWEST_SLUG)
    data = blog.breadcrumbs_jsonld(post, "https://persona.example.ru")
    items = data["itemListElement"]
    assert [i["position"] for i in items] == [1, 2, 3, 4]
    assert [i["name"] for i in items] == ["Главная", "Блог", "Память", post.title]
    assert items[2]["item"].endswith("/blog/category/pamyat")
    assert all(i["item"].startswith("https://persona.example.ru") for i in items)
    blog.reload_posts()


def test_howto_is_emitted_for_a_guide_with_numbered_steps(
    corpus_dir, monkeypatch
) -> None:
    post = _post(corpus_dir, monkeypatch, NEWEST_SLUG)
    data = blog.howto_jsonld(post, "https://persona.example.ru")
    assert data is not None
    assert data["@type"] == "HowTo"
    steps = data["step"]
    assert [s["name"] for s in steps] == ["Шаг 1. Установить Ollama", "Шаг 2. Скачать модель"]
    assert steps[0]["url"].endswith("#shag-1-ustanovit-ollama")
    blog.reload_posts()


def test_howto_is_not_invented_for_an_article_without_steps(
    corpus_dir, monkeypatch
) -> None:
    post = _post(corpus_dir, monkeypatch, OLDEST_SLUG)
    assert blog.howto_jsonld(post, "https://x") is None
    blog.reload_posts()


def test_itemlist_for_a_listing_page(corpus_dir, monkeypatch) -> None:
    monkeypatch.setattr(blog, "CONTENT_DIR", corpus_dir)
    monkeypatch.setattr(blog, "_cache_file", lambda: None)
    blog.reload_posts()
    _name, posts = blog.posts_in_category("pamyat")
    data = blog.itemlist_jsonld(posts, "https://persona.example.ru", "Память")
    assert data["@type"] == "ItemList"
    assert data["numberOfItems"] == len(posts)
    assert data["itemListElement"][0]["position"] == 1
    assert data["itemListElement"][0]["url"].startswith("https://persona.example.ru/blog/")
    blog.reload_posts()


def test_base_url_resolution_is_shared_with_the_sitemap() -> None:
    """One helper, so canonicals, JSON-LD, feeds and sitemap cannot disagree."""
    headers = {"host": "loopback:8000", "x-forwarded-host": "persona.example.ru",
               "x-forwarded-proto": "https"}
    assert blog.resolve_base_url(headers) == "https://persona.example.ru"
    assert blog.resolve_base_url({"host": "a.ru"}, request_scheme="http") == "http://a.ru"
    assert blog.resolve_base_url(
        {}, fallback_host="127.0.0.1", fallback_port=8000
    ) == "http://127.0.0.1:8000"
    # A comma-separated forwarded chain takes the first hop.
    assert blog.resolve_base_url(
        {"x-forwarded-host": "a.ru, b.ru", "x-forwarded-proto": "https, http"}
    ) == "https://a.ru"


@pytest.mark.asyncio
async def test_post_page_hands_templates_every_jsonld_block(client) -> None:
    """The data contract the post template codes against."""
    response = await client.get(f"/blog/{NEWEST_SLUG}")
    assert response.status_code == 200
    post = blog.get_post(NEWEST_SLUG)
    blocks = blog.post_jsonld(post, "http://testserver")
    assert [b["@type"] for b in blocks] == [
        "TechArticle",
        "BreadcrumbList",
        "FAQPage",
        "HowTo",
    ]
