"""Blog engine — file-based, public, SEO-first.

Articles live as Markdown files in ``app/web/content/blog/*.md`` with a small
``---`` front-matter block. They are SITE content (authored by the project),
NOT per-user data, so there is no DB table and no user_id scoping — the blog
is global and public (in the auth-gate allow-list under /blog).

Each post is rendered to HTML server-side (markdown-it-py) so search engines
get real content. We also:
  * inject **stable slug ids** on ``h2``/``h3`` headings (plus a legacy
    ``sec-{N}`` alias anchor, see :func:`_render_body`),
  * build a table of contents (for the sticky right-side TOC + scrollspy),
  * compute reading time,
  * extract the ``## Частые вопросы`` block into structured Q/A pairs so the
    post page can emit ``FAQPage`` JSON-LD,
  * build an in-process search index over titles/headings/tags/body.

Why this file is shaped the way it is (measured, not assumed)
------------------------------------------------------------
The corpus is growing from 28 to ~350 articles. Measured on a generated
350-file / 12.6 MB corpus on the reference Windows host:

===============================================  =========  ========
what                                             before     after
===============================================  =========  ========
first ``/blog`` (cold process, no cache)          9 555 ms   1 132 ms
first ``/blog`` (cold process, warm disk cache)   9 555 ms      35 ms
``/blog`` warm                                     ~0 ms       ~0 ms
one article page (first view)                    included      21 ms
first ``/blog/search`` (builds the index)              n/a   2 749 ms
subsequent searches                                    n/a      12 ms
===============================================  =========  ========

Three changes get there, in order of size:

1. **Nothing renders markdown on a listing path.** The old ``_all_posts``
   rendered every article to HTML while loading it; rendering all 350 costs
   5.6 s and ``/blog``, the feeds and the sitemap use none of it. HTML, TOC,
   FAQ and HowTo are now lazy per post (~21 ms once, cached for the process).
2. **The metadata for the whole corpus is cached in one JSON file** keyed by
   the content directory's ``(count, bytes, newest mtime)`` signature. What
   remained after (1) was not parsing — front matter for 350 files is 30 ms —
   it was 350 file opens plus UTF-8 decode of 12.6 MB. One 248 KB read
   replaces them. See :func:`_read_disk_cache`.
3. **A background warm-up thread** (:func:`warm_up_in_background`) pays even
   those costs before the first visitor arrives, and builds the search index,
   which is the one thing still worth 2.7 s (it has to read every body).

An on-disk cache of the rendered *HTML* was considered and rejected: after
(1) it would save 21 ms on the first view of each individual article, in
exchange for a multi-megabyte cache and a second invalidation rule.

Freshness: :func:`reload_posts` is no longer decorative. Every public entry
point calls :func:`_ensure_fresh`, which re-stats the content directory at
most once every :data:`_FRESHNESS_TTL` seconds and drops the caches when the
file count / total size / newest mtime changed. Editing a ``.md`` on disk is
therefore visible within a couple of seconds without a process restart, and
without spending a route from the (full) route budget on a reload endpoint.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any, Final

from markdown_it import MarkdownIt

from app.logging_setup import get_logger

log = get_logger("persona.blog")

CONTENT_DIR = Path(__file__).resolve().parent / "web" / "content" / "blog"

# words-per-minute for reading-time estimate (RU prose ~ 150-180)
_WPM = 170

# How often the content directory may be re-stat'ed for changes. One
# ``os.scandir`` over 350 entries costs ~1 ms, so this is free in practice;
# the throttle exists so a burst of requests does not turn into a burst of
# syscalls.
_FRESHNESS_TTL: Final[float] = 2.0

# Posts per page on every paginated listing (index, category, tag, search).
PAGE_SIZE: Final[int] = 24

# Default category for a post that does not declare one.
DEFAULT_CATEGORY: Final[str] = "Статьи"


# ---------------------------------------------------------------------------
# Transliteration + slugs
# ---------------------------------------------------------------------------

# Cyrillic → latin, matching the convention already used by every slug in
# ``docs/seo/content-map.csv`` (``что`` → ``chto``, ``личный`` → ``lichnyy``,
# ``квантизация`` → ``kvantizatsiya``). Category slugs, tag slugs and heading
# anchors all go through this, so a slug in a template, in the sitemap and in
# a canonical URL can never disagree.
_TRANSLIT: Final[dict[str, str]] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Ukrainian / Belarusian letters occasionally show up in quoted product
    # names; map them rather than dropping the whole word.
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u",
}

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Transliterate ``text`` to a stable ``a-z0-9-`` slug.

    Deterministic and total: any input yields a usable slug, and an input that
    transliterates to nothing yields ``""`` (callers decide what to do — for
    headings we fall back to the positional id).
    """
    lowered = (text or "").strip().lower()
    out: list[str] = []
    for ch in lowered:
        out.append(_TRANSLIT.get(ch, ch))
    return _SLUG_STRIP.sub("-", "".join(out)).strip("-")


def category_slug(name: str) -> str:
    """URL slug for a category name (``Своя модель`` → ``svoya-model``)."""
    return slugify(name) or "bez-kategorii"


def tag_slug(name: str) -> str:
    """URL slug for a tag name. Same rules as :func:`category_slug`."""
    return slugify(name) or "bez-tega"


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------

# The schema content authors are told to write. Unknown keys are kept in the
# raw mapping (so nothing crashes) but ignored by :func:`_load_one`; a key
# that is *close* to a known one is logged so a typo in one of 350 files is
# findable instead of silently doing nothing.
KNOWN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "title", "slug", "excerpt", "category", "tags", "keywords", "date",
        "updated", "cover", "image", "author", "featured", "noindex",
        # ``type`` is not in the style guide's table but IS in
        # docs/seo/content-map.csv (pillar/guide/comparison/...). Reading it
        # lets the post page emit HowTo markup for guides instead of guessing.
        "type", "related",
    }
)

_LIST_KEYS: Final[frozenset[str]] = frozenset({"tags", "keywords", "related"})
_BOOL_KEYS: Final[frozenset[str]] = frozenset({"featured", "noindex"})

_TRUE_WORDS: Final[frozenset[str]] = frozenset(
    {"1", "true", "yes", "y", "on", "да", "истина"}
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _strip_quotes(value: str) -> str:
    """Drop one layer of matching surrounding quotes.

    The old parser did not do this, so ``title: "Заголовок"`` rendered with
    its quotes in ``<title>``, in the ``<h1>``, in the JSON-LD and in the
    listing card. With 350 files being written by several authors this had to
    stop being the author's problem.
    """
    value = value.strip()
    if len(value) < 2:
        return value
    if value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    if value[0] == "«" and value[-1] == "»":
        return value[1:-1].strip()
    return value


def _split_list(value: str) -> list[str]:
    """``a, b, c`` or ``[a, b, c]`` → ``["a", "b", "c"]``."""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [_strip_quotes(part) for part in value.split(",") if _strip_quotes(part)]


def parse_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a leading ``---`` block from the body and parse it.

    Deliberately still hand-rolled: the project has no YAML dependency
    (``pyproject.toml`` declares none) and adding one to read eight scalar
    keys would be the most expensive way to fix quote-stripping. What this
    parser now supports, which the old one did not:

    * surrounding quotes are stripped (``"…"``, ``'…'``, ``«…»``);
    * block lists are understood::

          tags:
            - память
            - RAG

    * inline lists ``tags: [память, RAG]``;
    * booleans (``featured``, ``noindex``) in either language;
    * a UTF-8 BOM before the opening ``---``;
    * ``---`` closing fence with trailing spaces;
    * unknown keys are preserved in the mapping and never raise.

    Values keep the "split on the FIRST colon" rule, so a title like
    ``Persona vs Recall: чем отличается`` still parses correctly.
    """
    text = raw.lstrip("﻿")
    if not text.startswith("---"):
        return {}, raw
    # Find the closing fence: a line that is exactly "---" (trailing spaces ok).
    lines = text.splitlines()
    end_idx = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return {}, raw
    head_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")

    meta: dict[str, Any] = {}
    pending_list_key: str | None = None
    for line in head_lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        # Continuation of a block list (``  - память``).
        if stripped.startswith("- ") and pending_list_key:
            item = _strip_quotes(stripped[2:])
            if item:
                existing = meta.get(pending_list_key)
                if isinstance(existing, list):
                    existing.append(item)
                else:
                    meta[pending_list_key] = [item]
            continue
        if ":" not in stripped:
            pending_list_key = None
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        value = _strip_quotes(value)
        if not value and key in _LIST_KEYS:
            # ``tags:`` on its own line → a block list follows.
            pending_list_key = key
            meta[key] = []
            continue
        pending_list_key = None
        if key in _LIST_KEYS:
            meta[key] = _split_list(value)
        elif key in _BOOL_KEYS:
            meta[key] = value.strip().lower() in _TRUE_WORDS
        else:
            meta[key] = value
    return meta, body


# Kept under the old private name: ``docs/seo/style-guide.md`` and a couple of
# scripts refer to ``_parse_front_matter``.
_parse_front_matter = parse_front_matter


def _normalise_date(value: object, *, source: str, field_name: str) -> str:
    """Return an ISO ``YYYY-MM-DD`` string, or ``""`` when unusable.

    A malformed date does NOT drop the post — losing an article because
    someone typed ``2026-13-01`` would be a far worse failure than showing it
    without a date. We log it, loudly enough to grep, and move on.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    if not _DATE_RE.match(raw):
        log.warning("blog.bad_date", file=source, field=field_name, value=raw)
        return ""
    try:
        date.fromisoformat(raw)
    except ValueError:
        log.warning("blog.bad_date", file=source, field=field_name, value=raw)
        return ""
    return raw


# ---------------------------------------------------------------------------
# Markdown rendering, anchors, TOC, FAQ, HowTo
# ---------------------------------------------------------------------------

_LEGACY_ANCHOR_TOKEN: Final[str] = "persona_legacy_anchor"

_FAQ_HEADINGS: Final[frozenset[str]] = frozenset(
    {"частые вопросы", "часто задаваемые вопросы", "faq", "вопросы и ответы"}
)

_STEP_RE = re.compile(r"^\s*шаг\s*\d+", re.IGNORECASE)


def _render_legacy_anchor(
    tokens: list[Any], idx: int, _options: Any, _env: Any
) -> str:
    """Render the ``sec-{N}`` compatibility anchor for one heading.

    This is OUR token, produced by :func:`_render_body`, not markdown from the
    article — so emitting a tag here does not weaken ``{"html": False}``,
    which governs raw HTML in the *source*.
    """
    anchor_id = escape(str(tokens[idx].meta.get("id", "")), quote=True)
    return f'<span class="anchor-alias" id="{anchor_id}" aria-hidden="true"></span>\n'


def _build_md() -> MarkdownIt:
    # commonmark + tables + typographer; HTML disabled (we author trusted MD,
    # but keep it off as defence-in-depth). No linkify (avoids extra dep).
    md = (
        MarkdownIt("commonmark", {"html": False, "typographer": True})
        .enable("table")
        .enable("strikethrough")
    )
    md.renderer.rules[_LEGACY_ANCHOR_TOKEN] = _render_legacy_anchor
    return md


@dataclass(slots=True)
class TocItem:
    id: str  # stable transliterated slug — the canonical anchor
    title: str
    level: int  # 2 or 3
    legacy_id: str = ""  # the old positional ``sec-{N}``, still a live anchor


@dataclass(slots=True)
class FaqItem:
    question: str
    answer: str


@dataclass(slots=True)
class HowToStep:
    name: str
    text: str


@dataclass(slots=True)
class Taxon:
    """One category or tag: display name, URL slug, how many posts carry it."""

    name: str
    slug: str
    count: int


def _inline_text(token: Any) -> str:
    return (token.content if token is not None else "").strip()


def _plain_from_inline(tokens: list[Any], start: int, stop: int) -> str:
    """Concatenate the text of inline tokens in ``tokens[start:stop]``."""
    parts: list[str] = []
    for tok in tokens[start:stop]:
        if tok.type == "inline":
            parts.append(tok.content)
    return " ".join(p.strip() for p in parts if p.strip()).strip()


def _heading_ids(titles: Iterable[str]) -> list[str]:
    """Slug ids for heading titles, deduplicated with ``-2``, ``-3``, …."""
    seen: dict[str, int] = {}
    ids: list[str] = []
    for position, title in enumerate(titles):
        base = slugify(title) or f"sec-{position}"
        seen[base] = seen.get(base, 0) + 1
        ids.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return ids


def _render_body(body: str) -> tuple[str, list[TocItem], list[FaqItem], list[HowToStep]]:
    """Render markdown to HTML and extract TOC / FAQ / HowTo steps in one pass.

    Anchors
    -------
    Heading ids used to be positional (``sec-0``, ``sec-1``, …), so inserting
    one ``##`` in the middle of an article silently moved every anchor below
    it. Ids are now transliterated slugs of the heading text, deduplicated.

    The positional id is NOT dropped: each heading is preceded by an empty
    ``<span id="sec-{N}">`` so any ``#sec-3`` link that exists in the wild
    still lands on the right heading. It costs one empty span per heading and
    removes the whole class of "the anchor moved" bug reports. (The style
    guide currently forbids linking to section anchors at all *because* they
    were positional — that ban can now be lifted for the slug ids.)
    """
    md = _build_md()
    tokens = md.parse(body)

    heading_positions: list[int] = []
    heading_titles: list[str] = []
    for idx, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.tag in ("h2", "h3"):
            heading_positions.append(idx)
            inline = tokens[idx + 1] if idx + 1 < len(tokens) else None
            heading_titles.append(_inline_text(inline))
    ids = _heading_ids(heading_titles)

    toc: list[TocItem] = []
    for n, (pos, title, sid) in enumerate(
        zip(heading_positions, heading_titles, ids, strict=True)
    ):
        tok = tokens[pos]
        tok.attrSet("id", sid)
        toc.append(
            TocItem(id=sid, title=title, level=int(tok.tag[1]), legacy_id=f"sec-{n}")
        )

    faq = _extract_faq(tokens, heading_positions, heading_titles)
    steps = _extract_steps(tokens, heading_positions, heading_titles)

    # Insert the legacy alias anchors last, back-to-front, so the indices
    # collected above stay valid while we splice.
    from markdown_it.token import Token

    for n in range(len(heading_positions) - 1, -1, -1):
        alias = Token(_LEGACY_ANCHOR_TOKEN, "", 0)
        alias.block = True
        alias.meta = {"id": f"sec-{n}"}
        tokens.insert(heading_positions[n], alias)

    html = md.renderer.render(tokens, md.options, {})
    return html, toc, faq, steps


def _extract_faq(
    tokens: list[Any], heading_positions: list[int], heading_titles: list[str]
) -> list[FaqItem]:
    """Pull ``### question`` / answer pairs out of the ``## Частые вопросы`` block.

    The style guide (``docs/seo/style-guide.md`` §8) makes this block
    mandatory and fixes its shape precisely so it can be machine-read: one
    ``## Частые вопросы`` per article, 4–8 ``###`` questions, each answer
    self-contained. This is the highest-leverage structured-data win
    available — it is what produces the expandable FAQ rich result.

    Returns ``[]`` when there is no FAQ block, when it is empty, or when the
    article is malformed. Never raises: a broken FAQ block must not take the
    article's page down with it.
    """
    faq_start: int | None = None
    faq_end = len(tokens)
    for n, (pos, title) in enumerate(zip(heading_positions, heading_titles, strict=True)):
        if tokens[pos].tag != "h2":
            continue
        normalised = title.strip().lower().rstrip("?:").strip()
        if normalised in _FAQ_HEADINGS:
            faq_start = pos
            # the block ends at the next h2
            for later_pos in heading_positions[n + 1 :]:
                if tokens[later_pos].tag == "h2":
                    faq_end = later_pos
                    break
            break
    if faq_start is None:
        return []

    questions: list[tuple[int, str]] = [
        (pos, title)
        for pos, title in zip(heading_positions, heading_titles, strict=True)
        if faq_start < pos < faq_end and tokens[pos].tag == "h3"
    ]
    out: list[FaqItem] = []
    for i, (pos, question) in enumerate(questions):
        stop = questions[i + 1][0] if i + 1 < len(questions) else faq_end
        # skip the heading's own inline token (pos, pos+1, pos+2 = open/inline/close)
        answer = _plain_from_inline(tokens, pos + 3, stop)
        if question and answer:
            out.append(FaqItem(question=question, answer=answer))
    return out


def _extract_steps(
    tokens: list[Any], heading_positions: list[int], heading_titles: list[str]
) -> list[HowToStep]:
    """Collect ``## Шаг N …`` sections as HowTo steps.

    The guide template in the style guide (§2.3) prescribes exactly this
    shape (``## Шаг 1`` … ``## Шаг N``), so detection is a literal match on
    the convention rather than a guess about "numbered-looking" content. When
    an article has no such headings we return ``[]`` and the post simply gets
    no HowTo markup — inventing steps out of an arbitrary ordered list would
    put a wrong recipe in front of a search engine.
    """
    steps: list[HowToStep] = []
    step_headings = [
        (n, pos, title)
        for n, (pos, title) in enumerate(zip(heading_positions, heading_titles, strict=True))
        if _STEP_RE.match(title)
    ]
    for i, (n, pos, title) in enumerate(step_headings):
        if i + 1 < len(step_headings):
            stop = step_headings[i + 1][1]
        elif n + 1 < len(heading_positions):
            stop = heading_positions[n + 1]
        else:
            stop = len(tokens)
        text = _plain_from_inline(tokens, pos + 3, stop)
        steps.append(HowToStep(name=title, text=text[:1200]))
    return steps


# ---------------------------------------------------------------------------
# The post
# ---------------------------------------------------------------------------

_MONTHS_RU: Final[tuple[str, ...]] = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

_render_lock = threading.RLock()


def _human_date(iso: str) -> str:
    try:
        y, m, d = (int(x) for x in iso.split("-"))
        return f"{d} {_MONTHS_RU[m - 1]} {y}"
    except Exception:
        return iso


@dataclass(slots=True)
class BlogPost:
    slug: str
    title: str
    excerpt: str
    category: str
    tags: list[str]
    keywords: str
    date: str  # ISO yyyy-mm-dd
    cover: str  # emoji
    read_minutes: int
    word_count: int
    # v2 front matter
    updated: str = ""
    image: str = ""
    author: str = ""
    featured: bool = False
    noindex: bool = False
    type: str = ""
    related: list[str] = field(default_factory=list)
    # Where the markdown came from. ``source`` is the bare filename (it goes
    # into log lines); ``source_path`` is what the lazy body reader opens.
    source: str = field(default="", repr=False, compare=False)
    source_path: str = field(default="", repr=False, compare=False)
    # Lazily-filled caches (see the module docstring for why).
    _body: str | None = field(default=None, repr=False, compare=False)
    _html: str | None = field(default=None, repr=False, compare=False)
    _toc: list[TocItem] | None = field(default=None, repr=False, compare=False)
    _faq: list[FaqItem] | None = field(default=None, repr=False, compare=False)
    _steps: list[HowToStep] | None = field(default=None, repr=False, compare=False)

    # -- lazy body --------------------------------------------------------
    @property
    def body(self) -> str:
        """Markdown source below the front matter, read on demand.

        A post restored from the metadata cache has never had its file
        opened. Only three things need the body — rendering the article page,
        building the search index, and cutting a search snippet — and none of
        them happen on ``/blog``, ``/sitemap*.xml`` or the feeds.
        """
        if self._body is not None:
            return self._body
        try:
            raw = Path(self.source_path).read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("blog.body_unreadable", file=self.source, error=str(exc))
            self._body = ""
            return ""
        _meta, body = parse_front_matter(raw)
        self._body = body
        return body

    # -- lazy render ------------------------------------------------------
    def _ensure_rendered(self) -> None:
        if self._html is not None:
            return
        with _render_lock:
            if self._html is not None:
                return
            try:
                html, toc, faq, steps = _render_body(self.body)
            except Exception as exc:
                log.warning("blog.render_failed", file=self.source, error=str(exc))
                html, toc, faq, steps = "", [], [], []
            self._html = html
            self._toc = toc
            self._faq = faq
            self._steps = steps

    @property
    def html(self) -> str:
        self._ensure_rendered()
        return self._html or ""

    @property
    def toc(self) -> list[TocItem]:
        self._ensure_rendered()
        return self._toc or []

    @property
    def faq(self) -> list[FaqItem]:
        """``## Частые вопросы`` as structured Q/A pairs (may be empty)."""
        self._ensure_rendered()
        return self._faq or []

    @property
    def howto_steps(self) -> list[HowToStep]:
        """``## Шаг N`` sections, for HowTo markup on guides (may be empty)."""
        self._ensure_rendered()
        return self._steps or []

    # -- derived metadata (no rendering) ----------------------------------
    @property
    def date_human(self) -> str:
        return _human_date(self.date)

    @property
    def updated_human(self) -> str:
        return _human_date(self.updated) if self.updated else ""

    @property
    def last_modified(self) -> str:
        """``updated`` when present, else ``date``. Drives ``dateModified``."""
        return self.updated or self.date

    @property
    def category_slug(self) -> str:
        return category_slug(self.category)

    @property
    def tag_slugs(self) -> list[tuple[str, str]]:
        """``[(display_name, slug), …]`` so a template never slugifies itself."""
        return [(t, tag_slug(t)) for t in self.tags]

    @property
    def keyword_list(self) -> list[str]:
        return [k.strip() for k in self.keywords.split(",") if k.strip()]

    @property
    def is_guide(self) -> bool:
        return self.type.strip().lower() == "guide" or bool(self.howto_steps)

    @property
    def path(self) -> str:
        return f"/blog/{self.slug}"


def _load_one(path: Path) -> BlogPost | None:
    """Parse one ``.md`` file into a :class:`BlogPost` (metadata only).

    Returns ``None`` — with a WARNING naming the file — when the post cannot
    be loaded. The old behaviour was to return ``None`` silently on a missing
    title, which with 350 files means a single typo removes an article from
    the site and nobody ever finds out.
    """
    name = path.name
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("blog.unreadable", file=name, error=str(exc))
        return None

    meta, body = parse_front_matter(raw)
    if not meta:
        log.warning("blog.no_front_matter", file=name)
        return None

    title = str(meta.get("title") or "").strip()
    if not title:
        log.warning(
            "blog.missing_title",
            file=name,
            keys=sorted(str(k) for k in meta),
        )
        return None

    unknown = sorted(str(k) for k in meta if k not in KNOWN_KEYS)
    if unknown:
        # Not fatal — but a key nobody reads is almost always a typo, and at
        # corpus scale "it just didn't do anything" is unfindable otherwise.
        log.warning("blog.unknown_front_matter_keys", file=name, keys=unknown)

    tags_raw = meta.get("tags", [])
    tags = tags_raw if isinstance(tags_raw, list) else _split_list(str(tags_raw))
    keywords_raw = meta.get("keywords", "")
    keywords = (
        ", ".join(keywords_raw) if isinstance(keywords_raw, list) else str(keywords_raw)
    )
    related_raw = meta.get("related", [])
    related = related_raw if isinstance(related_raw, list) else _split_list(str(related_raw))

    excerpt = str(meta.get("excerpt", "")).strip() or _auto_excerpt(body)
    # Word count: ``str.split`` rather than ``re.findall(r"\w+")``. Measured on
    # a 350-file corpus that is 851 ms → 217 ms on the cold path, for a 2.2 %
    # difference in the count (table cells and code tokens count as words in
    # both, just slightly differently). A reading-time estimate does not earn
    # 0.6 s of every cold start.
    words = len(body.split())

    return BlogPost(
        slug=str(meta.get("slug") or "").strip() or path.stem,
        title=title,
        excerpt=excerpt,
        category=str(meta.get("category", "")).strip() or DEFAULT_CATEGORY,
        tags=tags,
        keywords=keywords,
        date=_normalise_date(meta.get("date"), source=name, field_name="date"),
        cover=str(meta.get("cover", "")).strip() or "📝",
        read_minutes=max(1, round(words / _WPM)),
        word_count=words,
        updated=_normalise_date(meta.get("updated"), source=name, field_name="updated"),
        image=str(meta.get("image", "")).strip(),
        author=str(meta.get("author", "")).strip(),
        featured=bool(meta.get("featured", False)),
        noindex=bool(meta.get("noindex", False)),
        type=str(meta.get("type", "")).strip(),
        related=related,
        source=name,
        source_path=str(path),
        _body=body,
    )


_MD_NOISE = re.compile(r"[*_`#>\[\]()!|]")


def _auto_excerpt(body: str, limit: int = 200) -> str:
    """First real paragraph, de-marked-up, as a fallback ``<meta description>``."""
    for chunk in body.split("\n\n"):
        text = chunk.strip()
        if not text or text.startswith(("#", "|", "```", ">", "-", "*")):
            continue
        text = _MD_NOISE.sub("", text).strip()
        if len(text) <= limit:
            return text
        cut = text[:limit].rsplit(" ", 1)[0]
        return f"{cut}…"
    return ""


# ---------------------------------------------------------------------------
# Corpus cache + freshness
# ---------------------------------------------------------------------------

_cache_lock = threading.RLock()
_posts_cache: list[BlogPost] | None = None
_public_cache: list[BlogPost] | None = None
_index_cache: _SearchIndex | None = None
_signature: tuple[int, int, int] | None = None
_signature_checked_at: float = 0.0


def _dir_signature() -> tuple[int, int, int]:
    """``(file_count, total_bytes, newest_mtime_ns)`` for the content dir.

    One ``os.scandir`` — ~1 ms for 350 entries. Any edit, addition or deletion
    moves at least one of the three numbers, which is what makes
    :func:`reload_posts` fire without a process restart.
    """
    count = 0
    total = 0
    newest = 0
    try:
        with os.scandir(CONTENT_DIR) as entries:
            for entry in entries:
                if not entry.name.endswith(".md"):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                count += 1
                total += int(stat.st_size)
                newest = max(newest, int(stat.st_mtime_ns))
    except OSError:
        return (0, 0, 0)
    return (count, total, newest)


def _ensure_fresh() -> None:
    """Drop the caches when the content directory changed on disk."""
    global _signature, _signature_checked_at
    now = time.monotonic()
    if _posts_cache is not None and now - _signature_checked_at < _FRESHNESS_TTL:
        return
    signature = _dir_signature()
    with _cache_lock:
        _signature_checked_at = now
        if signature != _signature:
            _signature = signature
            _drop_caches()


def _drop_caches() -> None:
    global _posts_cache, _public_cache, _index_cache
    _posts_cache = None
    _public_cache = None
    _index_cache = None


# ---------------------------------------------------------------------------
# On-disk metadata cache
# ---------------------------------------------------------------------------
#
# Why this exists (measured on the reference Windows host):
#
#   74 real articles, metadata-only cold load ........... 2 259 ms
#   → extrapolated to the planned ~350 articles ......... ~10 s
#
# That is what a visitor pays on the first ``/blog`` after every deploy or
# restart, before the background warm-up has finished. The cost is NOT
# parsing (front matter for 350 files is 30 ms) — it is 350 individual file
# opens plus UTF-8 decode of ~12 MB, and no amount of parser cleverness makes
# that cheaper.
#
# So the whole corpus's *metadata* is written to ONE small JSON file next to
# the database (``~/.persona/cache/``, outside the repo per CLAUDE.md), keyed
# by the same ``(file_count, total_bytes, newest_mtime_ns)`` signature that
# already drives hot reload. Restoring it is one open plus one ``json.loads``
# of ~250 KB instead of 350 opens of ~35 KB.
#
# Bodies are deliberately NOT cached. Storing them would make the cache file
# as large as the corpus and put us right back where we started; instead
# ``BlogPost.body`` reads its own file on demand, which only the article
# page, the search-index build and a search snippet ever trigger.
#
# Failure is always non-fatal: an unreadable, truncated, stale or
# wrong-version cache falls through to a normal scan. The file can be deleted
# at any time and the only consequence is one slow load.

_CACHE_VERSION: Final[int] = 1

#: Fields persisted per post. Anything derived (``date_human``,
#: ``category_slug``, the render caches) is recomputed, never stored.
_CACHED_FIELDS: Final[tuple[str, ...]] = (
    "slug", "title", "excerpt", "category", "tags", "keywords", "date",
    "cover", "read_minutes", "word_count", "updated", "image", "author",
    "featured", "noindex", "type", "related", "source", "source_path",
)


def _cache_file() -> Path | None:
    """Path of the metadata cache, or ``None`` when we cannot place one.

    Keyed by the content directory so a test that repoints ``CONTENT_DIR``
    can never collide with the real corpus's cache — and so two Persona
    installs sharing a data dir cannot poison each other.
    """
    try:
        from app.settings import get_settings

        base = Path(get_settings().data_dir).expanduser() / "cache"
    except Exception:
        return None
    key = slugify(str(CONTENT_DIR).replace(os.sep, "/")) or "blog"
    return base / f"blog-index-v{_CACHE_VERSION}-{key[-60:]}.json"


def _read_disk_cache(signature: tuple[int, int, int]) -> list[BlogPost] | None:
    """Rebuild the post list from the on-disk cache, or ``None``."""
    path = _cache_file()
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != _CACHE_VERSION:
        return None
    if tuple(payload.get("signature") or ()) != signature:
        # Corpus changed since the cache was written — the only correct answer
        # is to re-scan. A partial refresh would need per-file bookkeeping to
        # be safe, and a wrong article on /blog is worse than a slow one.
        return None
    rows = payload.get("posts")
    if not isinstance(rows, list):
        return None
    posts: list[BlogPost] = []
    try:
        for row in rows:
            posts.append(BlogPost(**{name: row[name] for name in _CACHED_FIELDS}))
    except (TypeError, KeyError, ValueError):
        log.warning("blog.cache_shape_mismatch", path=str(path))
        return None
    return posts


def _write_disk_cache(signature: tuple[int, int, int], posts: list[BlogPost]) -> None:
    """Persist post metadata. Best-effort — never raises into a request."""
    path = _cache_file()
    if path is None:
        return
    payload = {
        "version": _CACHE_VERSION,
        "signature": list(signature),
        "posts": [
            {name: getattr(post, name) for name in _CACHED_FIELDS} for post in posts
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a process reading the cache while another writes
        # it must never see half a JSON document.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("blog.cache_write_failed", path=str(path), error=str(exc))


def _all_posts() -> list[BlogPost]:
    """Every loadable post, newest first. Metadata only — HTML stays lazy."""
    global _posts_cache
    cached = _posts_cache
    if cached is not None:
        return cached
    with _cache_lock:
        if _posts_cache is not None:
            return _posts_cache
        started = time.perf_counter()
        if not CONTENT_DIR.exists():
            log.warning("blog.content_dir_missing", path=str(CONTENT_DIR))
            _posts_cache = []
            return _posts_cache

        signature = _signature if _signature is not None else _dir_signature()
        from_disk = _read_disk_cache(signature)
        if from_disk is not None:
            log.info(
                "blog.corpus_from_cache",
                posts=len(from_disk),
                ms=round((time.perf_counter() - started) * 1000),
            )
            _posts_cache = from_disk
            return from_disk

        files = sorted(CONTENT_DIR.glob("*.md"))
        posts = [p for p in (_load_one(f) for f in files) if p]
        # newest first; undated sink to the bottom
        posts.sort(key=lambda p: p.date or "0", reverse=True)
        skipped = len(files) - len(posts)
        log.info(
            "blog.corpus_loaded",
            files=len(files),
            posts=len(posts),
            skipped=skipped,
            ms=round((time.perf_counter() - started) * 1000),
        )
        _posts_cache = posts
        # Re-stat AFTER reading: content agents (and the operator's editor)
        # write files while we are mid-scan, and stamping the cache with the
        # pre-scan signature would freeze a corpus we never actually read.
        _write_disk_cache(_dir_signature(), posts)
        return posts


def reload_posts() -> None:
    """Drop every cache (posts + search index).

    Called automatically by :func:`_ensure_fresh` when the directory changes;
    exported for tests and for anything that edits content in-process.
    """
    global _signature, _signature_checked_at
    with _cache_lock:
        _drop_caches()
        _signature = None
        _signature_checked_at = 0.0


# ---------------------------------------------------------------------------
# Public listing API
# ---------------------------------------------------------------------------


def list_posts(*, include_hidden: bool = False) -> list[BlogPost]:
    """All published posts, newest first.

    ``noindex`` posts are excluded by default — from listings, feeds, the
    sitemap and search. They stay reachable at their own URL (and via
    :func:`get_post`), which is what ``noindex`` means: "serve it, don't
    advertise it".

    The filtered list is cached by identity, not rebuilt per call: the search
    index keys its validity on ``is`` against this exact list object, so a
    fresh list every call would rebuild the index on every query.
    """
    global _public_cache
    _ensure_fresh()
    posts = _all_posts()
    if include_hidden:
        return posts
    cached = _public_cache
    if cached is not None:
        return cached
    with _cache_lock:
        if _public_cache is None:
            _public_cache = [p for p in posts if not p.noindex]
        return _public_cache


def list_categories() -> list[str]:
    """Category display names in first-seen (newest-post) order."""
    seen: list[str] = []
    for p in list_posts():
        if p.category not in seen:
            seen.append(p.category)
    return seen


def categories() -> list[Taxon]:
    """Categories as ``Taxon(name, slug, count)``, most-populated first."""
    return _taxons((p.category,) for p in list_posts())


def tags() -> list[Taxon]:
    """Tags as ``Taxon(name, slug, count)``, most-populated first."""
    return _taxons(p.tags for p in list_posts())


def _taxons(groups: Iterable[Iterable[str]]) -> list[Taxon]:
    counts: dict[str, int] = {}
    for group in groups:
        for name in group:
            name = name.strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    out = [Taxon(name=n, slug=slugify(n) or "bez-imeni", count=c) for n, c in counts.items()]
    out.sort(key=lambda t: (-t.count, t.name))
    return out


def get_post(slug: str) -> BlogPost | None:
    """One post by slug. Finds ``noindex`` posts too — they are not secret."""
    _ensure_fresh()
    for p in _all_posts():
        if p.slug == slug:
            return p
    return None


def posts_in_category(slug: str) -> tuple[str, list[BlogPost]]:
    """``(display_name, posts)`` for a category slug. Empty list = unknown."""
    matched = [p for p in list_posts() if p.category_slug == slug]
    name = matched[0].category if matched else ""
    return name, matched


def posts_with_tag(slug: str) -> tuple[str, list[BlogPost]]:
    """``(display_name, posts)`` for a tag slug. Empty list = unknown."""
    matched: list[BlogPost] = []
    name = ""
    for post in list_posts():
        for tag_name, tslug in post.tag_slugs:
            if tslug == slug:
                matched.append(post)
                if not name:
                    name = tag_name
                break
    return name, matched


def related_posts(post: BlogPost, limit: int = 3) -> list[BlogPost]:
    """Explicit ``related:`` slugs first, then same-category, then same-tag.

    The old rule was "first three of the same category", which at 350 posts
    means every article in a big cluster points at the same three articles.
    """
    out: list[BlogPost] = []
    seen = {post.slug}

    def take(candidate: BlogPost) -> None:
        if candidate.slug not in seen and len(out) < limit:
            seen.add(candidate.slug)
            out.append(candidate)

    by_slug = {p.slug: p for p in list_posts()}
    for slug in post.related:
        target = by_slug.get(slug.strip())
        if target is not None:
            take(target)
    if len(out) < limit:
        for candidate in list_posts():
            if candidate.category == post.category:
                take(candidate)
    if len(out) < limit and post.tags:
        wanted = {t.lower() for t in post.tags}
        for candidate in list_posts():
            if wanted & {t.lower() for t in candidate.tags}:
                take(candidate)
    return out


def neighbours(post: BlogPost) -> tuple[BlogPost | None, BlogPost | None]:
    """``(previous_older, next_newer)`` by date, over the public listing."""
    posts = list_posts()
    try:
        idx = next(i for i, p in enumerate(posts) if p.slug == post.slug)
    except StopIteration:
        return None, None
    older = posts[idx + 1] if idx + 1 < len(posts) else None
    newer = posts[idx - 1] if idx - 1 >= 0 else None
    return older, newer


@dataclass(slots=True)
class Page:
    """One page of a paginated listing, with everything a template needs."""

    items: list[BlogPost]
    number: int
    total_pages: int
    total_items: int
    per_page: int

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.total_pages

    @property
    def prev_number(self) -> int | None:
        return self.number - 1 if self.has_prev else None

    @property
    def next_number(self) -> int | None:
        return self.number + 1 if self.has_next else None


def paginate(posts: list[BlogPost], page: int, per_page: int = PAGE_SIZE) -> Page:
    """Slice ``posts`` into :class:`Page` ``page`` (1-based).

    An empty collection is one empty page, not zero pages — "page 1 of 0" is
    a 404 waiting to happen on a brand-new category.
    """
    per_page = max(1, per_page)
    total_items = len(posts)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    number = max(1, page)
    start = (number - 1) * per_page
    return Page(
        items=posts[start : start + per_page],
        number=number,
        total_pages=total_pages,
        total_items=total_items,
        per_page=per_page,
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+")

# Field weights. A title hit outranks a heading hit outranks a tag outranks
# the body — the ordering the task fixes, with room between the tiers so a
# body full of a word can never out-score a title that contains it.
_W_TITLE: Final[int] = 120
_W_HEADING: Final[int] = 40
_W_TAG: Final[int] = 25
_W_KEYWORD: Final[int] = 18
_W_EXCERPT: Final[int] = 12
_W_BODY: Final[int] = 3
# A single word repeated 200 times in the body must not beat a heading.
_BODY_MAX_HITS: Final[int] = 6

# Score multipliers for the two inexact resolution paths (see ``_resolve``).
_PREFIX_FACTOR: Final[float] = 0.65
_TRUNCATION_FACTOR: Final[float] = 0.5
# Cap on how many index stems one query stem may expand to. Without it,
# a two-letter query would sum the whole corpus.
_MAX_PREFIX_EXPANSION: Final[int] = 60

# Common Russian inflectional endings, longest first. This is a truncated
# ending-stripper, NOT a stemmer: there is no Porter/Snowball here and no
# dictionary. It is honest about what it does — it folds the endings that
# actually differ between "память / памяти / памятью / памятями" and stops.
# What it misses (fleeting vowels, "статья / статей", verb stems) is caught
# by the prefix/truncation matching in ``_resolve``, which is why both exist.
_ENDINGS: Final[tuple[str, ...]] = (
    "иями", "ями", "ами", "ыми", "ими", "ого", "его", "ому", "ему", "ется",
    "ться", "тся", "ешь", "ишь", "ают", "яют", "ует", "уют", "ать", "ять",
    "ить", "еть", "ых", "их", "ая", "яя", "ое", "ее", "ые", "ие", "ый", "ий",
    "ой", "ей", "ов", "ев", "ам", "ям", "ах", "ях", "ом", "ем", "ью", "ия",
    "ие", "ии", "ию", "ла", "ло", "ли", "ть", "а", "я", "о", "е", "у", "ю",
    "ы", "и", "ь", "й",
)
# Never strip below this many characters — "или" must not become "и".
_MIN_STEM: Final[int] = 4


def normalise_token(token: str) -> str:
    """Lowercase, fold ``ё``→``е``. The shared entry point for index + query."""
    return token.lower().replace("ё", "е")


@lru_cache(maxsize=262_144)
def stem(token: str) -> str:
    """Fold one token to its search stem. See :data:`_ENDINGS` for the caveats.

    Memoised: indexing 350 articles means ~830k ``stem`` calls over a
    vocabulary of a few tens of thousands of distinct words, and the ending
    scan is pure Python. The cache cut index build from 3.4 s to 1.1 s
    (measured on the generated corpus).
    """
    token = normalise_token(token)
    if len(token) <= _MIN_STEM:
        return token
    for ending in _ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= _MIN_STEM:
            return token[: -len(ending)]
    return token


def _tokenise(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass(slots=True)
class _SearchIndex:
    postings: dict[str, dict[int, float]]
    stems: list[str]  # sorted, for prefix expansion
    posts: list[BlogPost]


@dataclass(slots=True)
class SearchHit:
    post: BlogPost
    score: float
    snippet: str
    matched: list[str]


_HEADING_RE = re.compile(r"^\s{0,3}#{2,3}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _build_index(posts: list[BlogPost]) -> _SearchIndex:
    started = time.perf_counter()
    postings: dict[str, dict[int, float]] = {}

    def add(idx: int, tokens: Iterable[str], weight: float, cap: int | None = None) -> None:
        counted: dict[str, int] = {}
        for raw in tokens:
            key = stem(raw)
            if not key:
                continue
            counted[key] = counted.get(key, 0) + 1
        for key, hits in counted.items():
            if cap is not None:
                hits = min(hits, cap)
            bucket = postings.setdefault(key, {})
            bucket[idx] = bucket.get(idx, 0.0) + weight * hits

    for idx, post in enumerate(posts):
        add(idx, _tokenise(post.title), _W_TITLE)
        # Headings are read straight off the markdown source: pulling them
        # here costs one regex pass and means building the index does NOT
        # force a full render of all 350 articles.
        heading_text = " ".join(_HEADING_RE.findall(post.body))
        add(idx, _tokenise(heading_text), _W_HEADING)
        add(idx, _tokenise(" ".join(post.tags)), _W_TAG)
        add(idx, _tokenise(post.keywords), _W_KEYWORD)
        add(idx, _tokenise(post.excerpt), _W_EXCERPT)
        add(idx, _tokenise(post.body), _W_BODY, cap=_BODY_MAX_HITS)

    index = _SearchIndex(postings=postings, stems=sorted(postings), posts=posts)
    log.info(
        "blog.index_built",
        posts=len(posts),
        stems=len(postings),
        ms=round((time.perf_counter() - started) * 1000),
    )
    return index


def _get_index() -> _SearchIndex:
    global _index_cache
    _ensure_fresh()
    cached = _index_cache
    posts = list_posts()
    if cached is not None and cached.posts is posts:
        return cached
    with _cache_lock:
        if _index_cache is not None and _index_cache.posts is posts:
            return _index_cache
        _index_cache = _build_index(posts)
        return _index_cache


def _bisect_prefix(stems: list[str], prefix: str) -> list[str]:
    """Index stems starting with ``prefix`` (bounded, sorted-list scan)."""
    import bisect

    start = bisect.bisect_left(stems, prefix)
    out: list[str] = []
    for candidate in stems[start : start + _MAX_PREFIX_EXPANSION]:
        if not candidate.startswith(prefix):
            break
        out.append(candidate)
    return out


def _resolve(index: _SearchIndex, query_stem: str) -> dict[int, float]:
    """Documents matching one query stem, with a confidence-scaled score.

    Three passes, in decreasing confidence — this is where Russian morphology
    is actually handled, and the reason it is three passes rather than "a
    stemmer" is that we do not have one and are not going to pretend:

    1. **exact** — the ending-stripper folded query and document to the same
       stem (``памятью`` and ``память`` both → ``памят``);
    2. **prefix** — the document's stem is longer than the query's
       (``статей`` → ``стат`` finds the indexed ``стать``);
    3. **truncation** — the document's stem is shorter than the query's
       (``статья`` → ``стать`` finds the indexed ``стат``), by walking the
       query stem back one character at a time down to :data:`_MIN_STEM`.

    Passes 2 and 3 are scaled down so an exact match always wins.
    """
    scores: dict[int, float] = {}
    exact = index.postings.get(query_stem)
    if exact:
        for doc, value in exact.items():
            scores[doc] = max(scores.get(doc, 0.0), value)
    for candidate in _bisect_prefix(index.stems, query_stem):
        if candidate == query_stem:
            continue
        for doc, value in index.postings[candidate].items():
            scaled = value * _PREFIX_FACTOR
            if scaled > scores.get(doc, 0.0):
                scores[doc] = scaled
    for cut in range(len(query_stem) - 1, _MIN_STEM - 1, -1):
        bucket = index.postings.get(query_stem[:cut])
        if not bucket:
            continue
        for doc, value in bucket.items():
            scaled = value * _TRUNCATION_FACTOR
            if scaled > scores.get(doc, 0.0):
                scores[doc] = scaled
        break
    return scores


def search(query: str, limit: int = 20) -> list[SearchHit]:
    """Rank public posts against ``query``.

    Ranking, in the order the task fixes: title hit > heading hit > tag >
    body, plus a bonus when the whole query appears verbatim in the title.
    A document must match **every** query term (AND) — with 350 articles an
    OR-search returns the whole corpus for any two-word query.

    ``noindex`` posts never appear: :func:`list_posts` filters them before
    the index is built.
    """
    terms = [t for t in _tokenise(query or "") if len(t) > 1]
    if not terms:
        return []
    index = _get_index()
    if not index.posts:
        return []

    running: dict[int, float] | None = None
    for term in terms:
        resolved = _resolve(index, stem(term))
        if not resolved:
            return []
        if running is None:
            running = dict(resolved)
        else:
            running = {
                doc: score + resolved[doc]
                for doc, score in running.items()
                if doc in resolved
            }
        if not running:
            return []
    assert running is not None

    phrase = normalise_token(query.strip())
    ranked: list[tuple[float, str, int]] = []
    for doc, score in running.items():
        post = index.posts[doc]
        if phrase and phrase in normalise_token(post.title):
            score += 250.0
        ranked.append((score, post.date or "0", doc))
    # Two stable sorts instead of one composite key: newest-first inside a
    # score tie, without inventing a "reversed string" comparison.
    ranked.sort(key=lambda row: row[1], reverse=True)
    ranked.sort(key=lambda row: row[0], reverse=True)

    hits: list[SearchHit] = []
    for score, _date, doc in ranked[: max(1, limit)]:
        post = index.posts[doc]
        hits.append(
            SearchHit(
                post=post,
                score=round(score, 2),
                snippet=_snippet(post, terms),
                matched=terms,
            )
        )
    return hits


def _snippet(post: BlogPost, terms: list[str], width: int = 220) -> str:
    """A plain-text window around the first matching term.

    Plain text on purpose: the template escapes it and does its own
    highlighting from ``SearchHit.matched``. Returning HTML from the engine
    would mean the engine owns the markup for a thing it cannot see.
    """
    body = post.body
    lowered = normalise_token(body)
    best = -1
    for term in terms:
        found = lowered.find(stem(term))
        if found != -1 and (best == -1 or found < best):
            best = found
    # Locate the match in the RAW body, then clean only the window around it.
    # Cleaning the whole article first cost a regex pass over ~35 KB per hit —
    # 20 hits per query made a warm search 40 ms instead of 2 ms.
    if best == -1:
        start, end = 0, min(len(body), width * 3)
    else:
        start = max(0, best - width)
        end = min(len(body), best + width * 2)
    window = _MD_NOISE.sub("", body[start:end]).replace("\n", " ")
    window = re.sub(r"\s{2,}", " ", window).strip()
    fragment = window[:width].strip()
    return ("…" if start > 0 else "") + fragment + ("…" if end < len(body) else "")


# ---------------------------------------------------------------------------
# Absolute URLs
# ---------------------------------------------------------------------------


def resolve_base_url(
    headers: Mapping[str, str],
    *,
    request_scheme: str = "http",
    fallback_host: str = "127.0.0.1",
    fallback_port: int = 8000,
) -> str:
    """Derive ``scheme://host`` for absolute URLs (canonical, JSON-LD, feeds).

    Single source of truth, shared by the sitemap, the feeds and the post
    page — three surfaces that must never disagree about what this site's
    canonical origin is. Honours ``X-Forwarded-Proto`` / ``X-Forwarded-Host``
    so a reverse proxy can publish on the public hostname even though uvicorn
    binds to loopback. ``app/web/routes/sitemap.py::_detect_base_url`` is a
    thin adapter over this.
    """
    forwarded_host = headers.get("x-forwarded-host")
    forwarded_proto = headers.get("x-forwarded-proto")
    host_header = headers.get("host")

    if forwarded_host:
        host = forwarded_host.split(",", 1)[0].strip()
    elif host_header:
        host = host_header.strip()
    else:
        host = f"{fallback_host}:{fallback_port}"

    if forwarded_proto:
        scheme = forwarded_proto.split(",", 1)[0].strip().lower()
    else:
        scheme = request_scheme or "http"
    return f"{scheme}://{host}"


def absolute(base_url: str, path: str) -> str:
    """Join ``base_url`` and a site-absolute ``path``. Pass through full URLs."""
    if path.startswith(("http://", "https://")):
        return path
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


# ---------------------------------------------------------------------------
# Structured data (JSON-LD payloads — dicts, not markup)
# ---------------------------------------------------------------------------

ORG_NAME: Final[str] = "Persona"


def _publisher(base_url: str) -> dict[str, Any]:
    return {"@type": "Organization", "name": ORG_NAME, "url": absolute(base_url, "/")}


def article_jsonld(post: BlogPost, base_url: str) -> dict[str, Any]:
    """``Article`` / ``TechArticle`` for one post, with absolute URLs.

    ``TechArticle`` for guides and troubleshooting (the types where Google
    actually treats it differently), plain ``Article`` otherwise.
    """
    url = absolute(base_url, post.path)
    kind = "TechArticle" if post.type.lower() in {"guide", "troubleshooting"} else "Article"
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": kind,
        "headline": post.title,
        "description": post.excerpt,
        "url": url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "inLanguage": "ru-RU",
        "wordCount": post.word_count,
        "articleSection": post.category,
        "publisher": _publisher(base_url),
        "author": {"@type": "Organization" if not post.author else "Person",
                   "name": post.author or ORG_NAME},
    }
    if post.date:
        data["datePublished"] = post.date
    if post.last_modified:
        data["dateModified"] = post.last_modified
    if post.keyword_list:
        data["keywords"] = post.keyword_list
    if post.image:
        data["image"] = [absolute(base_url, post.image)]
    return data


def breadcrumbs_jsonld(post: BlogPost, base_url: str) -> dict[str, Any]:
    """``BreadcrumbList``: Главная → Блог → Категория → статья."""
    trail = [
        ("Главная", absolute(base_url, "/")),
        ("Блог", absolute(base_url, "/blog")),
        (post.category, absolute(base_url, f"/blog/category/{post.category_slug}")),
        (post.title, absolute(base_url, post.path)),
    ]
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": name, "item": url}
            for i, (name, url) in enumerate(trail, start=1)
        ],
    }


def faq_jsonld(post: BlogPost) -> dict[str, Any] | None:
    """``FAQPage`` derived from ``## Частые вопросы``. ``None`` when absent.

    Returning ``None`` rather than an empty ``FAQPage`` matters: an FAQPage
    with zero mainEntity entries is a structured-data error in Search Console,
    which is worse than no markup at all.
    """
    if not post.faq:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": item.question,
                "acceptedAnswer": {"@type": "Answer", "text": item.answer},
            }
            for item in post.faq
        ],
    }


def howto_jsonld(post: BlogPost, base_url: str) -> dict[str, Any] | None:
    """``HowTo`` for a guide with ``## Шаг N`` sections. ``None`` otherwise."""
    steps = post.howto_steps
    if len(steps) < 2:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "HowTo",
        "name": post.title,
        "description": post.excerpt,
        "totalTime": f"PT{max(1, post.read_minutes)}M",
        "step": [
            {
                "@type": "HowToStep",
                "position": i,
                "name": step.name,
                "text": step.text or step.name,
                "url": f"{absolute(base_url, post.path)}#{slugify(step.name) or f'sec-{i}'}",
            }
            for i, step in enumerate(steps, start=1)
        ],
    }


def itemlist_jsonld(
    posts: list[BlogPost], base_url: str, name: str
) -> dict[str, Any]:
    """``ItemList`` for a listing page (index, category, tag, search)."""
    return {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": name,
        "numberOfItems": len(posts),
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": i,
                "url": absolute(base_url, post.path),
                "name": post.title,
            }
            for i, post in enumerate(posts, start=1)
        ],
    }


def post_jsonld(post: BlogPost, base_url: str) -> list[dict[str, Any]]:
    """Every JSON-LD block a post page should emit, ready for ``|tojson``."""
    blocks: list[dict[str, Any]] = [
        article_jsonld(post, base_url),
        breadcrumbs_jsonld(post, base_url),
    ]
    faq = faq_jsonld(post)
    if faq:
        blocks.append(faq)
    howto = howto_jsonld(post, base_url)
    if howto:
        blocks.append(howto)
    return blocks


# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------

_warmed = threading.Event()

#: One-shot latch: the first caller to acquire it owns the warm-up thread.
#: An :class:`~threading.Event` alone is not enough — it is only set when the
#: work *finishes*, so a burst of concurrent first requests would each see it
#: clear and spawn a thread of their own.
_warm_started = threading.Semaphore(1)


def warm_up() -> None:
    """Parse the corpus and build the search index, synchronously.

    Cheap to call twice — both stages are idempotent and cached.
    """
    list_posts()
    _get_index()
    _warmed.set()


def warm_up_in_background(delay: float = 0.0) -> None:
    """Kick :func:`warm_up` on a daemon thread. Idempotent, never blocks.

    Triggered by the FIRST request to any blog route, not at import.

    That distinction was measured, not assumed. The first version started
    this thread when ``app.web.routes.blog`` was imported, with a delay meant
    to let ``app.web.main`` finish importing first. It did not work: import
    of the whole app takes 5-13 s on the reference host, so the thread woke
    up mid-import and its ~2 s of GIL-heavy parsing landed squarely inside
    ``test_cold_web_import_stays_within_regression_budget``. Guessing a delay
    long enough to clear a variable-length import is not a design; it is a
    race with a timer.

    A startup hook would be the right place, but there is not one available
    here: ``app/web/main.py`` passes an explicit ``lifespan=``, and Starlette
    ignores router-level ``on_startup`` handlers once a lifespan is supplied.

    First-request triggering is better than both anyway. By then the process
    is serving, so the work competes with request handling rather than with
    startup — and the request that triggers it does not wait: it reads the
    metadata from the on-disk cache (~35 ms) while the thread builds the
    search index behind it. The index is ready seconds before anyone types
    into the search box.

    Disabled with ``PERSONA_BLOG_WARMUP=0``.
    """
    if os.environ.get("PERSONA_BLOG_WARMUP", "1") == "0":
        return
    if not _warm_started.acquire(blocking=False):
        return

    def _run() -> None:
        if delay:
            time.sleep(delay)
        try:
            warm_up()
        except Exception as exc:
            log.warning("blog.warmup_failed", error=str(exc))

    threading.Thread(target=_run, name="blog-warmup", daemon=True).start()


__all__ = [
    "PAGE_SIZE",
    "BlogPost",
    "FaqItem",
    "HowToStep",
    "Page",
    "SearchHit",
    "Taxon",
    "TocItem",
    "absolute",
    "article_jsonld",
    "breadcrumbs_jsonld",
    "categories",
    "category_slug",
    "faq_jsonld",
    "get_post",
    "howto_jsonld",
    "itemlist_jsonld",
    "list_categories",
    "list_posts",
    "neighbours",
    "paginate",
    "parse_front_matter",
    "post_jsonld",
    "posts_in_category",
    "posts_with_tag",
    "related_posts",
    "reload_posts",
    "resolve_base_url",
    "search",
    "slugify",
    "tag_slug",
    "tags",
    "warm_up",
    "warm_up_in_background",
]
