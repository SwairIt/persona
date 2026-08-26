"""Презентация блога: статья, листинги, микроразметка, секция на лендинге.

Соседние наборы стерегут ДВИЖОК (``test_blog_engine`` — парсинг, поиск,
пагинация) и САЙТ ЦЕЛИКОМ (``test_full_site_smoke`` — отсутствие 500,
``test_site_coherence`` — связность). Здесь сторожим то, что видит читатель,
и ровно те обещания, которые владелец дал вслух:

* сверху статьи есть индикатор прогресса чтения;
* страница поделена на текст и оглавление, у оглавления есть механизм
  «текущий раздел» — и он не сводится к одному цвету;
* в оглавлении столько же пунктов, сколько h2/h3 в тексте;
* у каждого заголовка есть кликабельный якорь;
* листинг переживает корпус в сотни статей: пагинация, рубрики и теги
  НАСТОЯЩИМИ ссылками, поиск, работающий без JS;
* микроразметка — валидный JSON с нужными полями;
* превью ссылки на статью не текстовое: есть og:image и twitter:card;
* каждый ``/static/`` из шаблонов блога лежит на диске и несёт ``?v=``
  (Service Worker кэширует эти файлы; без кэш-бастера читателю после
  релиза приезжает прошлая тема, и сбросить её он не может);
* секция «Из блога» на лендинге живёт по конвенциям лендинга
  (``.reveal`` / ``data-card`` / ``data-tilt`` / ``.section-title``).

Часть проверок идёт мимо HTTP — прямым рендером шаблона. Так тест описывает
контракт ШАБЛОНА (например «шесть карточек на лендинге»), не завися от того,
сколько статей роут решил передать сегодня.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import blog
from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv
from app.web.main import create_app
from app.web.routes import setup_gate
from app.web.templates_engine import templates

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC_ROOT = REPO_ROOT / "app" / "web" / "static"
TEMPLATES_DIR = REPO_ROOT / "app" / "web" / "templates"

#: Шаблоны, которыми владеет презентация блога.
BLOG_TEMPLATES = (
    "blog_base.html",
    "blog_index.html",
    "blog_post.html",
    "blog_category.html",
    "blog_tag.html",
    "blog_search.html",
    "blog_404.html",
    "_blog_macros.html",
)

_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_STATIC_REF = re.compile(r'(?:href|src)="(/static/[^"]+)"')
_T_CALL = re.compile(r"""(?<![A-Za-z0-9_.$])t\(\s*['"]""")


def _ld_blocks(html: str) -> list[dict[str, Any]]:
    """Все JSON-LD блоки страницы, уже разобранные. Битый JSON — падение."""
    return [json.loads(raw) for raw in _LD.findall(html)]


def _ld_of(html: str, *types: str) -> dict[str, Any]:
    for block in _ld_blocks(html):
        if block.get("@type") in types:
            return block
    present = [b.get("@type") for b in _ld_blocks(html)]
    raise AssertionError(f"нет JSON-LD блока типа {types}; на странице: {present}")


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    await init_database()
    owner_user = await create_user("owner@blogtpl.test", "Zq7-frost-lantern-91")
    async with get_connection() as conn:
        await set_kv(conn, "setup_complete", "true")
        await set_kv(conn, "owner_user_id", str(owner_user["id"]))
        await set_kv(conn, "owner_exclusive_mode", "0")
        await conn.commit()
    setup_gate._cache.mark_done()
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="module")
def corpus() -> list[blog.BlogPost]:
    posts = blog.list_posts()
    assert posts, "корпус блога пуст — тестировать нечего"
    return posts


# ── Статья ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_article_has_a_reading_progress_indicator(
    client: AsyncClient, corpus: list[blog.BlogPost]
) -> None:
    """Прогресс чтения — первое, что владелец просил вслух.

    Проверяем и разметку, и то, что её кто-то двигает: полоса без скрипта,
    меняющего её ширину, это декорация, а не индикатор.
    """
    body = (await client.get(f"/blog/{corpus[0].slug}")).text
    assert 'class="read-progress"' in body
    assert 'role="progressbar"' in body, "индикатор должен быть виден скринридеру"

    js = (STATIC_ROOT / "blog" / "blog.js").read_text(encoding="utf-8")
    assert ".read-progress" in js and "requestAnimationFrame" in js


@pytest.mark.asyncio
async def test_toc_has_one_entry_per_heading_and_an_active_state(
    client: AsyncClient, corpus: list[blog.BlogPost]
) -> None:
    """Оглавление = ровно заголовки статьи, и «текущий» отличим не цветом.

    Ловим два разных провала: рассинхрон списка с текстом (пункт ведёт в
    никуда) и «активный пункт», выраженный только оттенком серого.
    """
    post = next(p for p in corpus if len(p.toc) >= 3)
    body = (await client.get(f"/blog/{post.slug}")).text

    ids_in_text = re.findall(r'<h[23] id="([^"]+)"', body)
    ids_in_toc = re.findall(r'<a href="#([^"]+)" class="lvl-', body)
    assert ids_in_toc == ids_in_text, "оглавление и заголовки статьи разошлись"
    assert len(ids_in_toc) == len(post.toc)

    js = (STATIC_ROOT / "blog" / "blog.js").read_text(encoding="utf-8")
    css = (STATIC_ROOT / "blog" / "blog.css").read_text(encoding="utf-8")
    assert "is-active" in js and "IntersectionObserver" in js
    assert "is-read" in js, "прочитанные пункты обязаны отличаться от непрочитанных"
    assert ".post-toc a.is-active::before" in css, (
        "у активного пункта должна быть метка-рельс, а не только другой цвет"
    )
    assert "--toc-progress" in css and "--toc-progress" in js, (
        "внутри оглавления нет индикации прочитанного"
    )
    assert ".toc-scroll" in css and "overflow-y:auto" in css, (
        "у длинного оглавления должна быть собственная прокрутка"
    )


@pytest.mark.asyncio
async def test_toc_collapses_into_an_openable_control_on_phones(
    client: AsyncClient, corpus: list[blog.BlogPost]
) -> None:
    """На узком экране правая колонка раньше просто уезжала из виду.

    Переключатель — настоящий ``<input type=checkbox>``, поэтому ящик
    открывается и с выключенным JS; скрипт лишь закрывает его по выбору
    пункта и по Escape.
    """
    body = (await client.get(f"/blog/{corpus[0].slug}")).text
    assert '<input class="toc-check" type="checkbox" id="toc-toggle"' in body
    assert '<label class="toc-fab" for="toc-toggle"' in body
    assert 'class="toc-backdrop" for="toc-toggle"' in body
    assert 'class="toc-close" for="toc-toggle"' in body

    css = (STATIC_ROOT / "blog" / "blog.css").read_text(encoding="utf-8")
    assert ".toc-check:checked" in css
    assert "max-width:1080px" in css.replace(" ", ""), "нет мобильной раскладки"

    js = (STATIC_ROOT / "blog" / "blog.js").read_text(encoding="utf-8")
    assert "closeDrawer" in js and "Escape" in js


@pytest.mark.asyncio
async def test_every_heading_carries_a_clickable_anchor(
    client: AsyncClient, corpus: list[blog.BlogPost]
) -> None:
    """Якорь у заголовка ставится серверно, по ``TocItem.id``.

    Формат id не предполагаем (движок ушёл с позиционных ``sec-N`` на
    слаги) — сверяем ссылку с тем id, который реально стоит на заголовке.
    """
    post = next(p for p in corpus if len(p.toc) >= 3)
    body = (await client.get(f"/blog/{post.slug}")).text

    pairs = re.findall(
        r'<h[23] id="([^"]+)"><a class="anchor-link" href="#([^"]+)"', body
    )
    assert len(pairs) == len(post.toc), "не у каждого заголовка есть якорь"
    assert all(hid == href for hid, href in pairs)


@pytest.mark.asyncio
async def test_article_shows_dates_reading_time_tags_and_neighbours(
    client: AsyncClient, corpus: list[blog.BlogPost]
) -> None:
    """Метаданные статьи, теги под текстом и переходы к соседям."""
    post = next(p for p in corpus if p.tags)
    body = (await client.get(f"/blog/{post.slug}")).text

    assert f'<time datetime="{post.date}">' in body
    assert f"{post.read_minutes} мин чтения" in body
    assert 'class="post-tags"' in body
    for _name, slug in post.tag_slugs:
        assert f'href="/blog/tag/{slug}"' in body
    assert 'class="post-nav"' in body

    prev_post, next_post = blog.neighbours(post)
    if prev_post:
        assert f'href="/blog/{prev_post.slug}"' in body
    if next_post:
        assert f'href="/blog/{next_post.slug}"' in body


def test_article_renders_an_updated_date_when_the_post_has_one() -> None:
    """``updated`` — новое поле; шаблон обязан его показать, а не проглотить.

    Рендерим шаблон напрямую: ждать, пока в корпусе появится статья с датой
    обновления, — значит написать тест, который сегодня ничего не проверяет.
    """
    stub = blog.BlogPost(
        slug="stub-updated",
        title="Заголовок",
        excerpt="Короткое описание.",
        category="Технологии",
        tags=["память"],
        keywords="память",
        date="2026-01-05",
        cover="🧠",
        read_minutes=4,
        word_count=700,
        updated="2026-08-20",
        _body="## Раздел\n\nТекст.\n",
    )
    html = templates.env.get_template("blog_post.html").render(
        request=None,
        session=None,
        title="t",
        post=stub,
        prev_post=None,
        next_post=None,
        related=[],
    )
    assert 'class="updated"' in html
    assert '<time datetime="2026-08-20">' in html
    assert '<meta property="article:modified_time" content="2026-08-20" />' in html
    article = _ld_of(html, "Article", "TechArticle")
    assert article["dateModified"] == "2026-08-20"
    assert article["datePublished"] == "2026-01-05"


# ── Микроразметка и превью ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_article_structured_data_is_valid_and_complete(
    client: AsyncClient, corpus: list[blog.BlogPost]
) -> None:
    """Article/TechArticle + BreadcrumbList + FAQPage, все URL абсолютные."""
    post = next(p for p in corpus if p.faq)
    body = (await client.get(f"/blog/{post.slug}")).text

    article = _ld_of(body, "Article", "TechArticle")
    for field in ("headline", "datePublished", "dateModified", "wordCount", "image"):
        assert article.get(field), f"в Article нет поля {field}"
    assert article["dateModified"] == post.last_modified
    assert int(article["wordCount"]) == post.word_count
    assert str(article["url"]).startswith("http"), "URL в разметке обязан быть абсолютным"

    crumbs = _ld_of(body, "BreadcrumbList")
    items = crumbs["itemListElement"]
    assert [i["position"] for i in items] == list(range(1, len(items) + 1))
    assert items[-1]["name"] == post.title
    assert all(str(i["item"]).startswith("http") for i in items)

    faq = _ld_of(body, "FAQPage")
    assert len(faq["mainEntity"]) == len(post.faq)
    assert faq["mainEntity"][0]["acceptedAnswer"]["@type"] == "Answer"
    assert faq["mainEntity"][0]["name"].strip()


@pytest.mark.asyncio
async def test_article_link_preview_is_not_text_only(
    client: AsyncClient, corpus: list[blog.BlogPost]
) -> None:
    """og:image / og:url / twitter:card — иначе ссылка в мессенджере голая."""
    post = corpus[0]
    body = (await client.get(f"/blog/{post.slug}")).text

    og_image = re.search(r'<meta property="og:image" content="([^"]+)"', body)
    og_url = re.search(r'<meta property="og:url" content="([^"]+)"', body)
    assert og_image and og_image.group(1).startswith("http")
    assert og_url and og_url.group(1).endswith(f"/blog/{post.slug}")
    assert '<meta name="twitter:card" content="summary_large_image" />' in body
    assert '<meta name="twitter:image"' in body


@pytest.mark.asyncio
async def test_listing_pages_carry_an_itemlist_and_breadcrumbs(
    client: AsyncClient,
) -> None:
    """ItemList на рубрике — то, из чего вырастает расширенный сниппет."""
    taxon = blog.categories()[0]
    body = (await client.get(f"/blog/category/{taxon.slug}")).text

    assert _ld_blocks(body), "на странице рубрики нет микроразметки"
    listed = _ld_of(body, "ItemList", "CollectionPage")
    items = listed.get("itemListElement") or listed["mainEntity"]["itemListElement"]
    assert items and all(str(i["url"]).startswith("http") for i in items)
    _ld_of(body, "BreadcrumbList")


# ── Листинги ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_paginates_instead_of_dumping_the_whole_corpus(
    client: AsyncClient, corpus: list[blog.BlogPost]
) -> None:
    """Одна страница — один экран карточек, дальше пагинация.

    С корпусом в сотни статей «выведем все» — это мегабайт разметки и
    страница, которая не открывается на телефоне.
    """
    if len(corpus) <= blog.PAGE_SIZE:
        pytest.skip("корпус ещё меньше страницы — пагинации нечего показывать")

    first = (await client.get("/blog")).text
    # карточек на странице не больше размера страницы (+ полка «выбор редакции»)
    assert first.count('class="post-card"') <= blog.PAGE_SIZE + 3
    assert 'class="pager"' in first
    assert 'href="/blog?page=2"' in first

    second = (await client.get("/blog?page=2")).text
    assert 'aria-current="page"' in second
    assert second.count('class="post-card"') > 0

    # страница за пределом корпуса не должна плодить тонкие дубли
    assert (await client.get("/blog?page=999")).status_code == 404


@pytest.mark.asyncio
async def test_index_navigates_by_real_category_and_tag_links(
    client: AsyncClient,
) -> None:
    """Рубрики и теги — ссылки на живые роуты, а не клиентский фильтр.

    Для поисковика JS-фильтра не существует: до этого рубрик как страниц
    не было вовсе.
    """
    body = (await client.get("/blog")).text
    cats = blog.categories()
    tags = blog.tags()
    assert cats and tags

    for taxon in cats:
        assert f'href="/blog/category/{taxon.slug}"' in body
        assert (await client.get(f"/blog/category/{taxon.slug}")).status_code == 200

    shown = tags[:18]
    for taxon in shown:
        assert f'href="/blog/tag/{taxon.slug}"' in body
    assert (await client.get(f"/blog/tag/{shown[0].slug}")).status_code == 200

    # старых кнопок-фильтров быть не должно — они и были «невидимкой» для SEO
    assert 'class="cat-btn"' not in body


@pytest.mark.asyncio
async def test_search_works_without_javascript(client: AsyncClient) -> None:
    """Поиск — обычная GET-форма, результаты рисует сервер."""
    body = (await client.get("/blog")).text
    assert 'action="/blog/search" method="get"' in body
    assert 'name="q"' in body

    hit = (await client.get("/blog/search", params={"q": "память"})).text
    assert hit.count('class="post-card"') > 0
    assert "Найдено" in hit


@pytest.mark.asyncio
async def test_search_with_no_hits_offers_a_way_out(client: AsyncClient) -> None:
    """Пустая выдача — не пустой экран: объяснение и ссылки на рубрики."""
    body = (await client.get("/blog/search", params={"q": "щщщыыxzq"})).text
    assert 'class="post-card"' not in body
    assert 'data-empty-state="blog-search"' in body
    assert 'href="/blog/category/' in body
    assert 'href="/blog"' in body
    # выдача поиска не должна попадать в индекс
    assert '<meta name="robots" content="noindex, follow" />' in body


@pytest.mark.asyncio
async def test_category_tag_and_search_share_the_index_layout(
    client: AsyncClient,
) -> None:
    """Четыре экрана — один раздел, а не четыре разные страницы."""
    paths = (
        "/blog",
        f"/blog/category/{blog.categories()[0].slug}",
        f"/blog/tag/{blog.tags()[0].slug}",
        "/blog/search?q=память",
    )
    for path in paths:
        body = (await client.get(path)).text
        for marker in (
            'class="blog-main"',
            'class="blog-head"',
            'class="blog-search"',
            'class="post-grid"',
            'class="blog-count"',
            'class="footer"',
        ):
            assert marker in body, f"{path}: нет {marker}"


@pytest.mark.asyncio
async def test_unknown_post_gets_a_useful_404(client: AsyncClient) -> None:
    """404 блога — не тупик: поиск, рубрики и дорога обратно в блог."""
    r = await client.get("/blog/net-takoy-stati")
    assert r.status_code == 404
    assert 'class="blog-404"' in r.text
    assert 'action="/blog/search"' in r.text
    assert 'href="/blog"' in r.text


# ── Дизайн-система и ассеты ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blog_runs_on_the_landing_design_system(client: AsyncClient) -> None:
    """Блог грузит v2, а не v1: иначе шов между лендингом и блогом виден.

    Токены v1 (``landing/style.css``) на страницах блога — регресс: другой
    фон, другой шрифт, другие скругления и другой способ подъёма карточек.
    """
    for path in ("/blog", f"/blog/{blog.list_posts()[0].slug}"):
        body = (await client.get(path)).text
        assert "/static/landing_v2/style.css" in body, path
        assert "/static/landing/style.css" not in body, path
        assert 'class="nav nav--solid"' in body, path
        assert 'class="bh-fallback"' in body, path


@pytest.mark.asyncio
async def test_every_static_asset_of_the_blog_exists_and_is_cache_busted(
    client: AsyncClient,
) -> None:
    """У blog.css кэш-бастера не было вовсе, а его кэширует Service Worker.

    Читатель после релиза получал прошлую тему и не мог её сбросить иначе,
    чем вручную очистив кэш браузера.
    """
    paths = (
        "/blog",
        f"/blog/{blog.list_posts()[0].slug}",
        f"/blog/category/{blog.categories()[0].slug}",
        f"/blog/tag/{blog.tags()[0].slug}",
        "/blog/search?q=память",
        "/blog/net-takoy-stati",
    )
    problems: list[str] = []
    for path in paths:
        body = (await client.get(path)).text
        for ref in sorted(set(_STATIC_REF.findall(body))):
            file_part, _, query = ref.partition("?")
            if not (STATIC_ROOT / file_part[len("/static/") :]).exists():
                problems.append(f"{path}: {ref} — файла нет на диске")
            elif "v=" not in query:
                problems.append(f"{path}: {ref} — без ?v= (кэшируется навсегда)")
    assert not problems, "\n".join(problems)


def test_blog_templates_never_reintroduce_v1_tokens() -> None:
    """Ни один шаблон блога не должен снова тянуть дизайн-систему v1."""
    offenders = [
        name
        for name in BLOG_TEMPLATES
        if "/static/landing/style.css"
        in (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    ]
    assert not offenders, f"шаблоны вернулись на v1: {offenders}"


def test_public_blog_pages_are_hardcoded_russian() -> None:
    """Публичные страницы — без ``t()``: это конвенция проекта."""
    offenders = [
        name
        for name in BLOG_TEMPLATES
        if _T_CALL.search((TEMPLATES_DIR / name).read_text(encoding="utf-8"))
    ]
    assert not offenders, f"в публичных шаблонах блога появился t(): {offenders}"


def test_blog_javascript_respects_reduced_motion() -> None:
    """Плавная прокрутка и анимации выключаются по системной настройке."""
    js = (STATIC_ROOT / "blog" / "blog.js").read_text(encoding="utf-8")
    css = (STATIC_ROOT / "blog" / "blog.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in js
    assert "reduce ? 'auto' : 'smooth'" in js
    assert "prefers-reduced-motion" in css


# ── Секция «Из блога» на лендинге ──────────────────────────────────────────


def _home_blog_section(posts: list[blog.BlogPost]) -> str:
    html = templates.env.get_template("landing_v2.html").render(
        request=None, session=None, title="Persona", posts=posts
    )
    return html.split('<section class="home-blog"', 1)[1].split("</section>", 1)[0]


def test_landing_blog_section_shows_six_posts_and_follows_the_conventions() -> None:
    """Шесть карточек, ряд рубрик, ссылка на RSS — и всё по конвенциям v2.

    Рендерим шаблон напрямую с восемью статьями: тест описывает контракт
    СЕКЦИИ (шесть, а не три и не «сколько дали»), а не текущий срез роута.
    """
    posts = blog.list_posts()[:8]
    assert len(posts) >= 7, "для проверки среза нужно минимум 7 статей в корпусе"

    section = _home_blog_section(posts)
    assert section.count('class="hpost"') == 6, "секция обязана резать список до шести"
    assert section.count("data-card") == 6
    assert section.count("data-tilt") == 6
    assert 'class="section-title reveal"' in section
    assert 'class="section-lead reveal"' in section
    assert 'class="home-blog-cats reveal"' in section
    assert "/blog/rss.xml" in section
    assert 'href="/blog"' in section

    # Ссылка рубрики строится по СЛАГУ. Отображаемое имя в URL («Основы»)
    # выглядит рабочим и даёт стабильный 404: роут ищет рубрику по слагу.
    known = {taxon.slug for taxon in blog.categories()}
    linked = re.findall(r'href="/blog/category/([^"]+)"', section)
    assert linked, "в секции нет ни одной ссылки на рубрику"
    assert set(linked) <= known, f"рубрики без слага: {sorted(set(linked) - known)}"


@pytest.mark.asyncio
async def test_landing_blog_category_links_actually_resolve(
    client: AsyncClient,
) -> None:
    """Каждая рубрика, на которую зовёт лендинг, реально открывается."""
    body = (await client.get("/landing")).text
    section = body.split('<section class="home-blog"', 1)[1].split("</section>", 1)[0]
    for slug in sorted(set(re.findall(r'href="/blog/category/([^"]+)"', section))):
        r = await client.get(f"/blog/category/{slug}")
        assert r.status_code == 200, f"/blog/category/{slug} → {r.status_code}"
    assert (await client.get("/blog/rss.xml")).status_code == 200


def test_landing_blog_section_survives_a_short_post_list() -> None:
    """Меньше шести статей — секция всё равно рисуется, без дыр."""
    section = _home_blog_section(blog.list_posts()[:2])
    assert section.count('class="hpost"') == 2
    assert 'class="home-blog-cats reveal"' in section
