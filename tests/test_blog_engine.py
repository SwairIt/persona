"""Blog engine: front matter, anchors, search, pagination, freshness, scale.

Companion set: ``tests/test_blog_seo.py`` covers the crawler-facing surface
(routes, feeds, sitemap, structured data). This file stays below HTTP.

Every test drives :data:`app.blog.CONTENT_DIR` at a temp directory rather
than the real corpus — content agents add articles continuously, so any
assertion against the shipped ``.md`` files would be flaky by construction.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from app import blog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the engine at an empty temp dir and hand back a writer.

    The on-disk metadata cache is disabled here: these tests rewrite the same
    file repeatedly inside one second, and a cache keyed on
    ``(count, bytes, mtime_ns)`` can legitimately consider two different
    one-line edits identical at that resolution. The cache has its own tests
    below, which drive it deliberately.
    """
    content = tmp_path / "blog"
    content.mkdir()
    monkeypatch.setattr(blog, "CONTENT_DIR", content)
    monkeypatch.setattr(blog, "_cache_file", lambda: None)
    blog.reload_posts()

    def write(name: str, text: str) -> Path:
        path = content / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        blog.reload_posts()
        return path

    yield write
    blog.reload_posts()


def article(
    *,
    title: str = "Обычная статья про память",
    slug: str = "obychnaya",
    extra: str = "",
    body: str = "Текст статьи про память и контекст.",
) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        f"slug: {slug}\n"
        "excerpt: Короткое описание без воды.\n"
        "category: Память\n"
        "tags: память, RAG\n"
        "keywords: память ии, rag\n"
        "date: 2026-06-07\n"
        "cover: 🧠\n"
        f"{extra}"
        "---\n\n"
        f"{body}\n"
    )


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------


def test_surrounding_quotes_are_stripped(corpus) -> None:
    """``title: "X"`` used to render with its quotes in <title> and JSON-LD."""
    corpus(
        "quoted",
        article(title='"Квантизация LLM: что терять не жалко"', slug="quoted"),
    )
    post = blog.get_post("quoted")
    assert post is not None
    assert post.title == "Квантизация LLM: что терять не жалко"
    assert '"' not in post.title


def test_single_and_guillemet_quotes_are_stripped(corpus) -> None:
    corpus("q1", article(title="'Одинарные кавычки'", slug="q1"))
    corpus("q2", article(title="«Ёлочки вокруг всего»", slug="q2"))
    assert blog.get_post("q1").title == "Одинарные кавычки"
    assert blog.get_post("q2").title == "Ёлочки вокруг всего"


def test_colon_inside_a_title_still_parses(corpus) -> None:
    corpus("colon", article(title="Persona vs Recall: чем отличается", slug="colon"))
    assert blog.get_post("colon").title == "Persona vs Recall: чем отличается"


def test_missing_title_skips_the_post_loudly(corpus, capsys) -> None:
    """Silently dropping an article is unfindable across 350 files."""
    (blog.CONTENT_DIR / "broken.md").write_text(
        "---\nslug: broken\ncategory: Память\n---\n\nТекст.\n", encoding="utf-8"
    )
    blog.reload_posts()
    posts = blog.list_posts()
    logged = capsys.readouterr().out
    assert [p.slug for p in posts] == []
    assert "blog.missing_title" in logged, f"no warning; got {logged!r}"
    assert "broken.md" in logged, "the warning must name the file"


def test_unknown_keys_do_not_crash_and_are_reported(corpus, capsys) -> None:
    corpus("unknown", article(slug="unknown", extra="totaly_not_a_key: 42\nauthr: Я\n"))
    post = blog.get_post("unknown")
    logged = capsys.readouterr().out
    assert post is not None
    assert post.title  # the post still loads
    assert "blog.unknown_front_matter_keys" in logged


def test_new_schema_keys_are_read(corpus) -> None:
    corpus(
        "v2",
        article(
            slug="v2",
            extra=(
                "updated: 2026-08-20\n"
                "image: /static/blog/og/v2.png\n"
                "author: Ярослав\n"
                "featured: yes\n"
                "noindex: false\n"
                "type: guide\n"
            ),
        ),
    )
    post = blog.get_post("v2")
    assert post.updated == "2026-08-20"
    assert post.last_modified == "2026-08-20"
    assert post.image == "/static/blog/og/v2.png"
    assert post.author == "Ярослав"
    assert post.featured is True
    assert post.noindex is False
    assert post.type == "guide"


def test_malformed_date_keeps_the_post_and_logs(corpus, capsys) -> None:
    """A typo'd date must not delete an article from the site."""
    raw = article(slug="baddate").replace("date: 2026-06-07", "date: 07.06.2026")
    (blog.CONTENT_DIR / "baddate.md").write_text(raw, encoding="utf-8")
    blog.reload_posts()
    post = blog.get_post("baddate")
    logged = capsys.readouterr().out
    assert post is not None
    assert post.date == ""
    assert post.date_human == ""
    assert "blog.bad_date" in logged


def test_out_of_range_date_is_rejected(corpus) -> None:
    raw = article(slug="m13").replace("date: 2026-06-07", "date: 2026-13-01")
    (blog.CONTENT_DIR / "m13.md").write_text(raw, encoding="utf-8")
    blog.reload_posts()
    assert blog.get_post("m13").date == ""


def test_block_and_inline_tag_lists(corpus) -> None:
    block = (
        "---\n"
        "title: Блочный список тегов\n"
        "slug: blocklist\n"
        "excerpt: e\n"
        "category: Память\n"
        "tags:\n"
        "  - память\n"
        "  - RAG\n"
        "keywords: [память ии, rag]\n"
        "date: 2026-06-07\n"
        "cover: 🧠\n"
        "---\n\nТекст.\n"
    )
    (blog.CONTENT_DIR / "blocklist.md").write_text(block, encoding="utf-8")
    blog.reload_posts()
    post = blog.get_post("blocklist")
    assert post.tags == ["память", "RAG"]
    assert post.keyword_list == ["память ии", "rag"]


def test_bom_and_trailing_space_fence(corpus) -> None:
    raw = "﻿" + article(slug="bom").replace("---\n\n", "---   \n\n", 1)
    (blog.CONTENT_DIR / "bom.md").write_text(raw, encoding="utf-8")
    blog.reload_posts()
    assert blog.get_post("bom") is not None


def test_missing_excerpt_falls_back_to_the_first_paragraph(corpus) -> None:
    raw = article(slug="noexc", body="Первый абзац статьи, он и станет описанием.")
    raw = raw.replace("excerpt: Короткое описание без воды.\n", "")
    (blog.CONTENT_DIR / "noexc.md").write_text(raw, encoding="utf-8")
    blog.reload_posts()
    assert blog.get_post("noexc").excerpt.startswith("Первый абзац")


def test_file_without_front_matter_is_skipped(corpus, capsys) -> None:
    (blog.CONTENT_DIR / "plain.md").write_text("Просто текст\n", encoding="utf-8")
    blog.reload_posts()
    assert blog.list_posts() == []
    assert "blog.no_front_matter" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------


def test_heading_ids_are_transliterated_slugs(corpus) -> None:
    corpus(
        "anchors",
        article(
            slug="anchors",
            body="## Контекстное окно\n\nТекст.\n\n### Почему это важно\n\nТекст.\n",
        ),
    )
    post = blog.get_post("anchors")
    assert [t.id for t in post.toc] == ["kontekstnoe-okno", "pochemu-eto-vazhno"]
    assert [t.level for t in post.toc] == [2, 3]
    assert 'id="kontekstnoe-okno"' in post.html


def test_anchor_ids_are_stable_when_a_heading_is_inserted(corpus) -> None:
    """The whole point: adding a section must not move the anchors below it."""
    corpus("stable", article(slug="stable", body="## Первый\n\nA\n\n## Третий\n\nB\n"))
    before = [t.id for t in blog.get_post("stable").toc]
    corpus(
        "stable",
        article(slug="stable", body="## Первый\n\nA\n\n## Второй\n\nX\n\n## Третий\n\nB\n"),
    )
    after = [t.id for t in blog.get_post("stable").toc]
    assert before == ["pervyy", "tretiy"]
    assert after == ["pervyy", "vtoroy", "tretiy"]
    # "третий" kept its anchor even though its position changed 1 → 2.
    assert after[2] == before[1]


def test_duplicate_headings_are_deduplicated(corpus) -> None:
    corpus(
        "dupes",
        article(slug="dupes", body="## Итоги\n\nA\n\n## Итоги\n\nB\n\n## Итоги\n\nC\n"),
    )
    assert [t.id for t in blog.get_post("dupes").toc] == ["itogi", "itogi-2", "itogi-3"]


def test_legacy_positional_anchors_still_resolve(corpus) -> None:
    """External ``#sec-1`` links keep landing on the same heading."""
    corpus("legacy", article(slug="legacy", body="## Первый\n\nA\n\n## Второй\n\nB\n"))
    post = blog.get_post("legacy")
    assert [t.legacy_id for t in post.toc] == ["sec-0", "sec-1"]
    assert 'id="sec-0"' in post.html
    assert 'id="sec-1"' in post.html
    # …and the alias sits immediately before its heading, not somewhere random.
    assert post.html.index('id="sec-1"') < post.html.index('id="vtoroy"')


def test_heading_that_transliterates_to_nothing_falls_back(corpus) -> None:
    corpus("empty", article(slug="empty", body="## ???\n\nТекст.\n"))
    ids = [t.id for t in blog.get_post("empty").toc]
    assert ids == ["sec-0"]


def test_raw_html_in_the_source_is_still_disabled(corpus) -> None:
    """The legacy-anchor renderer must not have re-opened raw HTML."""
    corpus("xss", article(slug="xss", body="<script>alert(1)</script>\n"))
    assert "<script>" not in blog.get_post("xss").html


# ---------------------------------------------------------------------------
# FAQ / HowTo extraction (the data side; markup is tested in test_blog_seo)
# ---------------------------------------------------------------------------


FAQ_BODY = """Вступление статьи.

## Как это устроено

Разбор механики.

## Частые вопросы

### Чем эмбеддинг отличается от обычного текста?

Эмбеддинг — это смысл текста, записанный числами.

### RAG — это и есть долговременная память ИИ?

RAG — это механизм извлечения, а не само хранилище.

## Вывод

Если вам нужна память — берите RAG.
"""


def test_faq_block_is_extracted(corpus) -> None:
    corpus("faq", article(slug="faq", body=FAQ_BODY))
    faq = blog.get_post("faq").faq
    assert len(faq) == 2
    assert faq[0].question == "Чем эмбеддинг отличается от обычного текста?"
    assert "числами" in faq[0].answer
    assert faq[1].question.startswith("RAG")
    # The block ends at the next h2 — "Вывод" must not become an answer.
    assert "берите RAG" not in faq[1].answer


def test_article_without_faq_block_has_no_faq(corpus) -> None:
    corpus("nofaq", article(slug="nofaq", body="## Раздел\n\nТекст.\n"))
    assert blog.get_post("nofaq").faq == []


def test_howto_steps_are_extracted_for_guides(corpus) -> None:
    body = (
        "## Что понадобится\n\nOllama.\n\n"
        "## Шаг 1. Установить Ollama\n\nСкачайте установщик.\n\n"
        "## Шаг 2. Скачать модель\n\nВыполните ollama pull.\n\n"
        "## Проверка результата\n\nМодель отвечает.\n"
    )
    corpus("guide", article(slug="guide", extra="type: guide\n", body=body))
    post = blog.get_post("guide")
    steps = post.howto_steps
    assert [s.name for s in steps] == ["Шаг 1. Установить Ollama", "Шаг 2. Скачать модель"]
    assert "Скачайте установщик" in steps[0].text
    assert "Модель отвечает" not in steps[1].text
    assert post.is_guide is True


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


def test_category_and_tag_slugs_are_stable_transliterations() -> None:
    assert blog.category_slug("Своя модель") == "svoya-model"
    assert blog.category_slug("Технологии") == "tehnologii"
    assert blog.tag_slug("локальные модели") == "lokalnye-modeli"
    assert blog.tag_slug("VRAM") == "vram"
    # Same helper drives the sitemap and the templates, so the slug a post
    # reports must equal the slug the taxonomy lists.
    assert blog.slugify("Приватность") == "privatnost"


def test_taxonomy_counts_and_lookup(corpus) -> None:
    corpus("a", article(slug="a"))
    corpus(
        "b",
        article(slug="b", title="Вторая").replace("category: Память", "category: Гайды"),
    )
    cats = {c.slug: c.count for c in blog.categories()}
    assert cats == {"pamyat": 1, "gaydy": 1}
    name, posts = blog.posts_in_category("pamyat")
    assert name == "Память"
    assert [p.slug for p in posts] == ["a"]
    assert blog.posts_in_category("net-takoy") == ("", [])
    name, posts = blog.posts_with_tag("rag")
    assert name == "RAG"
    assert len(posts) == 2


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.fixture
def search_corpus(corpus):
    corpus(
        "title-hit",
        article(
            title="Память ИИ и как она устроена",
            slug="title-hit",
            body="Статья о векторах и поиске.\n",
        ),
    )
    corpus(
        "heading-hit",
        article(
            title="Локальный запуск моделей",
            slug="heading-hit",
            body="## Память и контекст\n\nВводный текст про запуск.\n",
        ),
    )
    corpus(
        "body-hit",
        article(
            title="Выбор видеокарты",
            slug="body-hit",
            body="Здесь слово память встречается один раз в теле статьи.\n",
        ).replace("tags: память, RAG", "tags: железо"),
    )
    return corpus


def test_ranking_title_beats_heading_beats_body(search_corpus) -> None:
    hits = blog.search("память", limit=10)
    order = [h.post.slug for h in hits]
    assert order.index("title-hit") < order.index("heading-hit") < order.index("body-hit")


def test_search_is_case_and_yo_insensitive(search_corpus) -> None:
    assert blog.search("ПАМЯТЬ")[0].post.slug == "title-hit"
    assert blog.search("Память")[0].post.slug == "title-hit"


@pytest.mark.parametrize(
    "query", ["память", "памяти", "памятью", "памятей", "ПАМЯТЯХ"]
)
def test_russian_inflections_find_the_same_article(search_corpus, query: str) -> None:
    """The ending-stripper plus prefix/truncation fallback, end to end."""
    hits = blog.search(query, limit=5)
    assert hits, f"{query!r} found nothing"
    assert hits[0].post.slug == "title-hit"


def test_multi_word_query_requires_every_term(search_corpus) -> None:
    """OR-search returns the whole corpus for any two-word query."""
    # "квантизация" appears in no article in this corpus, so the AND must
    # eliminate every document even though "память" matches three of them.
    assert blog.search("память") != []
    assert blog.search("память квантизация") == []
    hits = blog.search("память устроена")
    assert [h.post.slug for h in hits] == ["title-hit"]


def test_search_returns_a_snippet_containing_context(search_corpus) -> None:
    hit = blog.search("векторах")[0]
    assert hit.post.slug == "title-hit"
    assert "вектор" in hit.snippet.lower()
    assert "<" not in hit.snippet, "the engine returns plain text; the template marks it"


def test_search_respects_the_limit_and_empty_query(search_corpus) -> None:
    assert blog.search("", limit=5) == []
    assert blog.search("   ", limit=5) == []
    assert len(blog.search("память", limit=1)) == 1


def test_noindex_posts_never_appear_in_search(corpus) -> None:
    corpus("hidden", article(slug="hidden", title="Служебная память", extra="noindex: 1\n"))
    assert blog.search("память") == []
    assert blog.list_posts() == []
    # …but the page itself is still servable.
    assert blog.get_post("hidden") is not None
    assert len(blog.list_posts(include_hidden=True)) == 1


def test_stemmer_never_truncates_short_words() -> None:
    """"или" must not become "и" — that would match half the corpus."""
    for word in ("или", "ии", "два", "код", "имя"):
        assert blog.stem(word) == word.replace("ё", "е")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _fake_posts(n: int) -> list[blog.BlogPost]:
    return [
        blog.BlogPost(
            slug=f"p{i}", title=f"T{i}", excerpt="", category="Память", tags=[],
            keywords="", date="2026-01-01", cover="📝", read_minutes=1, word_count=1,
        )
        for i in range(n)
    ]


@pytest.mark.parametrize(
    ("total", "page", "expect_len", "expect_pages"),
    [
        (0, 1, 0, 1),        # empty collection is ONE empty page, not zero
        (1, 1, 1, 1),
        (24, 1, 24, 1),      # exactly one full page
        (25, 1, 24, 2),
        (25, 2, 1, 2),       # boundary: the remainder page
        (48, 2, 24, 2),
        (49, 3, 1, 3),
    ],
)
def test_pagination_boundaries(total, page, expect_len, expect_pages) -> None:
    result = blog.paginate(_fake_posts(total), page)
    assert len(result.items) == expect_len
    assert result.total_pages == expect_pages
    assert result.total_items == total


def test_pagination_prev_next_flags() -> None:
    page = blog.paginate(_fake_posts(60), 2)
    assert (page.has_prev, page.has_next) == (True, True)
    assert (page.prev_number, page.next_number) == (1, 3)
    last = blog.paginate(_fake_posts(60), 3)
    assert (last.has_prev, last.has_next) == (True, False)
    assert last.next_number is None


def test_pagination_clamps_page_zero_and_negative() -> None:
    assert blog.paginate(_fake_posts(30), 0).number == 1
    assert blog.paginate(_fake_posts(30), -5).number == 1


# ---------------------------------------------------------------------------
# Freshness — reload_posts() wired to the filesystem
# ---------------------------------------------------------------------------


def test_editing_a_file_on_disk_is_picked_up_without_a_restart(
    corpus, monkeypatch
) -> None:
    """``reload_posts`` used to exist and never be called by anything."""
    monkeypatch.setattr(blog, "_FRESHNESS_TTL", 0.0)
    corpus("live", article(slug="live", title="Первая версия"))
    assert blog.get_post("live").title == "Первая версия"
    (blog.CONTENT_DIR / "live.md").write_text(
        article(slug="live", title="Вторая версия"), encoding="utf-8"
    )
    assert blog.get_post("live").title == "Вторая версия"


def test_adding_a_file_is_picked_up(corpus, monkeypatch) -> None:
    monkeypatch.setattr(blog, "_FRESHNESS_TTL", 0.0)
    corpus("one", article(slug="one"))
    assert len(blog.list_posts()) == 1
    (blog.CONTENT_DIR / "two.md").write_text(
        article(slug="two", title="Вторая"), encoding="utf-8"
    )
    assert len(blog.list_posts()) == 2


def test_freshness_check_is_throttled(corpus, monkeypatch) -> None:
    """Without the throttle this is an os.scandir per request per listing."""
    corpus("t", article(slug="t"))
    blog.list_posts()
    calls = {"n": 0}
    real = blog._dir_signature

    def counted():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(blog, "_dir_signature", counted)
    for _ in range(50):
        blog.list_posts()
    assert calls["n"] <= 1


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

_WORDS = (
    "память контекст модель локальная приватность токен эмбеддинг вектор "
    "запрос ответ агент инструмент квантизация видеокарта провайдер ключ"
).split()


def _generate_corpus(dest: Path, count: int = 350) -> None:
    """~350 realistic articles in a temp dir — never written into the repo."""
    rng = random.Random(20260825)
    cats = ["Основы", "Память", "Приватность", "Продуктивность", "Своя модель",
            "Сравнения", "Гайды", "Технологии"]
    for i in range(count):
        chunks: list[str] = []
        for section in range(rng.randint(4, 9)):
            chunks.append(f"## Раздел {section}: {' '.join(rng.choices(_WORDS, k=3))}")
            for _ in range(3):
                chunks.append(" ".join(rng.choices(_WORDS, k=70)) + ".")
            if section % 2 == 0:
                chunks.append("| Критерий | A | B |\n|---|---|---|\n| скорость | ✓ | ✗ |")
        chunks.append("## Частые вопросы")
        for q in range(5):
            chunks.append(f"### Вопрос {q} про {rng.choice(_WORDS)}?")
            chunks.append(" ".join(rng.choices(_WORDS, k=45)) + ".")
        head = (
            "---\n"
            f"title: Статья {i} про {rng.choice(_WORDS)}\n"
            f"slug: gen-{i}\n"
            f"excerpt: Описание статьи {i}.\n"
            f"category: {cats[i % len(cats)]}\n"
            f"tags: {', '.join(rng.sample(_WORDS, 4))}\n"
            f"keywords: {', '.join(rng.sample(_WORDS, 5))}\n"
            f"date: 2026-0{1 + i % 8}-{1 + i % 28:02d}\n"
            "cover: 🧠\n"
            "---\n\n"
        )
        (dest / f"gen-{i}.md").write_text(head + "\n\n".join(chunks) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def big_corpus(tmp_path_factory):
    path = tmp_path_factory.mktemp("blog-350")
    _generate_corpus(path)
    return path


# ---------------------------------------------------------------------------
# On-disk metadata cache
# ---------------------------------------------------------------------------


@pytest.fixture
def disk_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Enable the metadata cache, pointed at a throwaway file."""
    content = tmp_path / "blog"
    content.mkdir()
    cache = tmp_path / "cache" / "idx.json"
    monkeypatch.setattr(blog, "CONTENT_DIR", content)
    monkeypatch.setattr(blog, "_cache_file", lambda: cache)
    blog.reload_posts()
    yield content, cache
    blog.reload_posts()


def test_metadata_cache_round_trips(disk_cache) -> None:
    content, cache = disk_cache
    (content / "a.md").write_text(
        article(slug="a", title="Кэшируемая статья", extra="author: Ярослав\n"),
        encoding="utf-8",
    )
    blog.reload_posts()
    first = blog.list_posts()
    assert cache.exists(), "the cold scan must leave a cache behind"

    blog.reload_posts()
    second = blog.list_posts()
    assert [p.slug for p in second] == [p.slug for p in first]
    restored = second[0]
    assert restored.title == "Кэшируемая статья"
    assert restored.author == "Ярослав"
    assert restored.tags == ["память", "RAG"]
    # The body was NOT cached — it is read from the file on demand.
    assert restored._body is None
    assert "Текст статьи" in restored.body


def test_cache_is_ignored_when_the_corpus_changed(disk_cache, monkeypatch) -> None:
    content, _cache = disk_cache
    monkeypatch.setattr(blog, "_FRESHNESS_TTL", 0.0)
    (content / "a.md").write_text(article(slug="a"), encoding="utf-8")
    blog.reload_posts()
    assert len(blog.list_posts()) == 1
    (content / "b.md").write_text(article(slug="b", title="Вторая"), encoding="utf-8")
    assert len(blog.list_posts()) == 2, "a stale cache served a corpus that moved"


def test_corrupt_cache_falls_back_to_a_scan(disk_cache) -> None:
    """A truncated cache must cost one slow load, never a broken blog."""
    content, cache = disk_cache
    (content / "a.md").write_text(article(slug="a"), encoding="utf-8")
    blog.reload_posts()
    blog.list_posts()
    cache.write_text("{not json at all", encoding="utf-8")
    blog.reload_posts()
    assert [p.slug for p in blog.list_posts()] == ["a"]


def test_cache_from_a_future_version_is_ignored(disk_cache) -> None:
    import json

    content, cache = disk_cache
    (content / "a.md").write_text(article(slug="a"), encoding="utf-8")
    blog.reload_posts()
    blog.list_posts()
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["version"] = blog._CACHE_VERSION + 99
    cache.write_text(json.dumps(payload), encoding="utf-8")
    blog.reload_posts()
    assert [p.slug for p in blog.list_posts()] == ["a"]


def test_cache_makes_a_350_article_cold_load_an_order_of_magnitude_faster(
    big_corpus, tmp_path, monkeypatch
) -> None:
    """The measurement that justifies the cache existing at all.

    Reference numbers on the dev host: 1 132 ms scanning 350 files, 35 ms
    restoring them from the cache. The assertion is deliberately loose (5x,
    not 30x) — it is here to catch "the cache stopped being used", not to
    police CI timing noise.
    """
    cache = tmp_path / "cache.json"
    monkeypatch.setattr(blog, "CONTENT_DIR", big_corpus)
    monkeypatch.setattr(blog, "_cache_file", lambda: cache)
    blog.reload_posts()
    started = time.perf_counter()
    assert len(blog.list_posts()) == 350
    scan = time.perf_counter() - started

    blog.reload_posts()
    started = time.perf_counter()
    assert len(blog.list_posts()) == 350
    cached = time.perf_counter() - started
    blog.reload_posts()

    assert cached * 5 < scan, (
        f"cache gave no speed-up: scan {scan*1000:.0f} ms vs cached "
        f"{cached*1000:.0f} ms — is the cache being written/read at all?"
    )


#: Generous by design. The metadata-only cold load measured ~1.4 s for 350
#: files on the reference Windows host; this catches "someone made /blog
#: render all 350 articles again" (which measured 9.6 s), not CI jitter.
COLD_METADATA_BUDGET_SECONDS = 6.0


def test_cold_metadata_load_of_350_articles_stays_fast(
    big_corpus, monkeypatch
) -> None:
    """``/blog`` and ``/sitemap.xml`` must not pay for rendering 350 articles.

    The regression this guards is specific and has happened once already:
    touching ``.html`` (or ``.toc``, or ``.faq``) inside :func:`list_posts`
    or inside a listing route drags every article through markdown-it on the
    cold path and turns a 1.4 s first request into a 9.6 s one.
    """
    monkeypatch.setattr(blog, "CONTENT_DIR", big_corpus)
    monkeypatch.setattr(blog, "_cache_file", lambda: None)
    blog.reload_posts()
    started = time.perf_counter()
    posts = blog.list_posts()
    elapsed = time.perf_counter() - started
    blog.reload_posts()
    assert len(posts) == 350
    assert elapsed < COLD_METADATA_BUDGET_SECONDS, (
        f"cold metadata load of 350 articles took {elapsed:.2f}s "
        f"(budget {COLD_METADATA_BUDGET_SECONDS}s) — something on the listing "
        "path is now rendering markdown eagerly"
    )


def test_listing_a_large_corpus_does_not_render_html(big_corpus, monkeypatch) -> None:
    """The structural version of the timing test — no clock involved."""
    monkeypatch.setattr(blog, "CONTENT_DIR", big_corpus)
    monkeypatch.setattr(blog, "_cache_file", lambda: None)
    blog.reload_posts()
    posts = blog.list_posts()
    blog.paginate(posts, 1)
    blog.categories()
    blog.tags()
    assert all(p._html is None for p in posts), "a listing path forced a render"
    _ = posts[0].html
    assert posts[0]._html is not None
    assert sum(1 for p in posts if p._html is not None) == 1
    blog.reload_posts()


def test_warm_listing_of_350_articles_is_instant(big_corpus, monkeypatch) -> None:
    monkeypatch.setattr(blog, "CONTENT_DIR", big_corpus)
    monkeypatch.setattr(blog, "_cache_file", lambda: None)
    blog.reload_posts()
    blog.list_posts()
    started = time.perf_counter()
    for _ in range(50):
        blog.list_posts()
    elapsed = time.perf_counter() - started
    blog.reload_posts()
    assert elapsed < 1.0, f"50 warm listings took {elapsed:.2f}s"
