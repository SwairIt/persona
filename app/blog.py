"""Blog engine — file-based, public, SEO-first.

Articles live as Markdown files in ``app/web/content/blog/*.md`` with a small
``---`` front-matter block. They are SITE content (authored by the project),
NOT per-user data, so there is no DB table and no user_id scoping — the blog
is global and public (in the auth-gate allow-list under /blog).

Each post is rendered to HTML server-side (markdown-it-py) so search engines
get real content. We also:
  * inject stable ids on ``h2``/``h3`` headings,
  * build a table of contents (for the sticky right-side TOC + scrollspy),
  * compute reading time.

Posts are parsed once and cached in-process (cheap, content is static).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

from markdown_it import MarkdownIt

CONTENT_DIR = Path(__file__).resolve().parent / "web" / "content" / "blog"

# words-per-minute for reading-time estimate (RU prose ~ 150-180)
_WPM = 170


@dataclass(slots=True)
class TocItem:
    id: str
    title: str
    level: int  # 2 or 3


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
    html: str
    toc: list[TocItem]
    read_minutes: int
    word_count: int

    @property
    def date_human(self) -> str:
        try:
            y, m, d = (int(x) for x in self.date.split("-"))
            months = [
                "января", "февраля", "марта", "апреля", "мая", "июня",
                "июля", "августа", "сентября", "октября", "ноября", "декабря",
            ]
            return f"{d} {months[m - 1]} {y}"
        except Exception:
            return self.date


def _build_md() -> MarkdownIt:
    # commonmark + tables + typographer; HTML disabled (we author trusted MD,
    # but keep it off as defence-in-depth). No linkify (avoids extra dep).
    return (
        MarkdownIt("commonmark", {"html": False, "typographer": True})
        .enable("table")
        .enable("strikethrough")
    )


def _parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    """Split a leading ``---`` block of ``key: value`` lines from the body."""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    head = raw[3:end].strip()
    body = raw[end + 4 :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip().lower()] = v.strip()
    return meta, body


def _render_body(body: str) -> tuple[str, list[TocItem]]:
    md = _build_md()
    tokens = md.parse(body)
    toc: list[TocItem] = []
    for idx, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.tag in ("h2", "h3"):
            inline = tokens[idx + 1] if idx + 1 < len(tokens) else None
            title = (inline.content if inline else "").strip()
            sid = f"sec-{len(toc)}"
            tok.attrSet("id", sid)
            toc.append(TocItem(id=sid, title=title, level=int(tok.tag[1])))
    html = md.renderer.render(tokens, md.options, {})
    return html, toc


def _load_one(path: Path) -> BlogPost | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _parse_front_matter(raw)
    if not meta.get("title"):
        return None
    html, toc = _render_body(body)
    words = len(re.findall(r"\w+", body))
    tags = [t.strip() for t in meta.get("tags", "").split(",") if t.strip()]
    return BlogPost(
        slug=meta.get("slug") or path.stem,
        title=meta["title"],
        excerpt=meta.get("excerpt", ""),
        category=meta.get("category", "Статьи"),
        tags=tags,
        keywords=meta.get("keywords", ""),
        date=meta.get("date", ""),
        cover=meta.get("cover", "📝"),
        html=html,
        toc=toc,
        read_minutes=max(1, round(words / _WPM)),
        word_count=words,
    )


@lru_cache(maxsize=1)
def _all_posts() -> list[BlogPost]:
    if not CONTENT_DIR.exists():
        return []
    posts = [p for p in (_load_one(f) for f in CONTENT_DIR.glob("*.md")) if p]
    # newest first; undated sink to the bottom
    posts.sort(key=lambda p: p.date or "0", reverse=True)
    return posts


def list_posts() -> list[BlogPost]:
    """All published posts, newest first."""
    return _all_posts()


def list_categories() -> list[str]:
    seen: list[str] = []
    for p in _all_posts():
        if p.category not in seen:
            seen.append(p.category)
    return seen


def get_post(slug: str) -> BlogPost | None:
    for p in _all_posts():
        if p.slug == slug:
            return p
    return None


def reload_posts() -> None:
    """Drop the cache (call after editing content on disk)."""
    _all_posts.cache_clear()
