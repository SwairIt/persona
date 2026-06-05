"""Hashtag auto-suggest from OCR — surface 5 candidate hashtags per shot.

Reads the OCR text, window title and app name of a single screenshot, tokenises
each field, drops a baked-in stopword set (English + Russian function words +
common OCR/web noise), and ranks the survivors by *term frequency in this shot*
weighted by *inverse document frequency* across the last :data:`_IDF_CORPUS_CAP`
shots. The top 5 tokens are returned along with the field they originated from
(``"ocr"``, ``"title"`` or ``"app"``) so the UI can render a hint.

The function is read-only — it never writes to ``screenshot_tags``. Persisting
selected tags is the job of :mod:`app.web.routes.hashtag_suggest`, which calls
the storage helpers directly with ``INSERT OR IGNORE`` (SQLite's spelling of
``ON CONFLICT DO NOTHING``).

Returns ``{"shot_id": int, "candidates": [{"tag": str, "score": float,
"source": "ocr"|"title"|"app"}, ...]}``.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Final, Literal, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.hashtag_suggest")


# Number of candidates returned by :func:`suggest_hashtags_for_shot`. Fixed at 5
# per the spec — exposed as a constant so tests and the widget template stay in
# sync if the cap ever moves.
TOP_K: Final[int] = 5

# Upper bound on the corpus used to compute IDF. A larger window gives smoother
# IDF but turns every suggest call into a full-table scan; 1000 shots is enough
# to drown out per-shot noise while staying snappy on a hot SQLite cache. Aligns
# with the ``last 1000 shots (cap)`` figure in the spec.
_IDF_CORPUS_CAP: Final[int] = 1000

# Minimum token length after cleaning. Shorter tokens are usually OCR noise
# (single letters, two-letter conjunctions) and would otherwise dominate the
# raw TF count.
_MIN_TOKEN_LENGTH: Final[int] = 3


Source = Literal["ocr", "title", "app"]


class Candidate(TypedDict):
    """One ranked hashtag candidate."""

    tag: str
    score: float
    source: Source


class SuggestResult(TypedDict):
    """Top-level response — kept JSON-friendly for the route layer."""

    shot_id: int
    candidates: list[Candidate]


# ---------------------------------------------------------------------------
# Stopwords — English + Russian function words plus common OCR / web noise.
# Mirrors the spirit of :data:`app.keywords.STOPWORDS` but is duplicated here so
# this module stays self-contained per the spec ("en + ru stopwords baked into
# the module"). Cyrillic stopwords live in a single space-separated literal so
# the ambiguous-glyph lints (RUF001 / RUF003) only fire once.
# ---------------------------------------------------------------------------

_RU_STOPWORDS_RAW: Final[str] = (
    "это что как для если или его ее её так "  # noqa: RUF001
    "уже там тут был была было были буду будет "
    "есть нет при над под без про ещё еще "
    "вот вам нам них ими ему ней том тем "
    "все всё себя себе меня тебя теперь когда "  # noqa: RUF001
    "очень может можно нужно надо будут "
    "только более менее также потом затем почему "
    "чтобы потому тогда здесь сюда туда везде "
    "никогда иногда часто редко много мало сколько "
    "однако вообще просто далее вместе всегда "
    "она оно они мы вы ты мне нас вас "
    "так же ли бы не да на по из от до "
    "ну то ни об об о за со в во к ко"  # noqa: RUF001
)
_RU_STOPWORDS: Final[tuple[str, ...]] = tuple(_RU_STOPWORDS_RAW.split())


STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        # English function words
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "can",
        "had",
        "her",
        "was",
        "one",
        "our",
        "out",
        "day",
        "get",
        "has",
        "him",
        "his",
        "how",
        "man",
        "new",
        "now",
        "old",
        "see",
        "two",
        "way",
        "who",
        "boy",
        "did",
        "its",
        "let",
        "put",
        "say",
        "she",
        "too",
        "use",
        "any",
        "ask",
        "big",
        "bad",
        "end",
        "few",
        "got",
        "lot",
        "off",
        "own",
        "set",
        "top",
        "via",
        "yes",
        "yet",
        "from",
        "this",
        "that",
        "with",
        "have",
        "they",
        "your",
        "will",
        "what",
        "when",
        "make",
        "like",
        "time",
        "just",
        "know",
        "take",
        "into",
        "year",
        "good",
        "some",
        "them",
        "than",
        "then",
        "look",
        "only",
        "come",
        "over",
        "also",
        "back",
        "after",
        "work",
        "well",
        "even",
        "want",
        "give",
        "most",
        "find",
        "tell",
        "very",
        "still",
        "such",
        "here",
        "thing",
        "many",
        "much",
        "more",
        "been",
        "were",
        "would",
        "could",
        "should",
        "about",
        "their",
        "there",
        "these",
        "those",
        "which",
        "where",
        "while",
        "shall",
        "every",
        "first",
        "other",
        "right",
        "think",
        "really",
        "before",
        "between",
        "because",
        "through",
        "during",
        "though",
        # Russian function words (Cyrillic — silenced inline above)
        *_RU_STOPWORDS,
        # Technical / OCR noise
        "http",
        "https",
        "www",
        "com",
        "org",
        "net",
        "html",
        "css",
        "url",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "pdf",
        "mp3",
        "mp4",
        "zip",
        "tar",
        "file",
        "files",
        "name",
        "type",
        "true",
        "false",
        "none",
        "null",
        "undefined",
        "var",
        "const",
        "return",
        "import",
        "export",
        "function",
        "class",
        "method",
        "object",
        "array",
        "string",
        "number",
        "boolean",
        "void",
        "self",
        "args",
        "kwargs",
        "param",
        "params",
        "value",
        "values",
        "data",
        "item",
        "items",
        "list",
        "dict",
        "key",
        "keys",
        "src",
        "href",
        "alt",
        "div",
        "span",
        "img",
        "form",
        "input",
        "button",
        "label",
        "head",
        "body",
        "main",
        "footer",
        "header",
        "section",
        "article",
        "aside",
        "table",
        "row",
        "col",
        "cell",
        "page",
        "pages",
        "site",
        "sites",
        "link",
        "links",
        "menu",
        "click",
        "open",
        "close",
        "save",
        "load",
        "edit",
        "view",
        "show",
        "hide",
        "next",
        "prev",
        "home",
        "filter",
        "sort",
        "submit",
    }
)


def _tokenise(text: str | None) -> list[str]:
    """Split on whitespace + punctuation, lowercase, drop short / noise tokens.

    Unicode-aware via :meth:`str.isalnum`, so Cyrillic survives the same code
    path as ASCII. Returns the cleaned token list ready for frequency counting;
    stopwords are still present here so the caller can decide whether to keep a
    title token that happens to be a stopword (we don't, but the separation
    keeps the helper reusable).
    """
    if not text:
        return []
    out: list[str] = []
    for raw in text.split():
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if cleaned:
            out.append(cleaned.lower())
    return out


def _meaningful(token: str) -> bool:
    """Return ``True`` if ``token`` is long enough and not a stopword."""
    return len(token) >= _MIN_TOKEN_LENGTH and token not in STOPWORDS


async def _load_shot(
    shot_id: int,
) -> tuple[str | None, str | None, str | None] | None:
    """Fetch ``ocr_text``, ``window_title`` and ``app_name`` for one screenshot.

    Returns ``None`` when no row exists so the caller can surface a 404 (the
    route does — this function itself is read-only and never raises).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ocr_text, window_title, app_name FROM screenshots WHERE id = ? LIMIT 1",
            (shot_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return (
        None if row["ocr_text"] is None else str(row["ocr_text"]),
        None if row["window_title"] is None else str(row["window_title"]),
        None if row["app_name"] is None else str(row["app_name"]),
    )


async def _idf_corpus_document_frequencies(
    shot_id: int,
) -> tuple[int, Counter[str]]:
    """Return ``(num_docs, df)`` across the most recent :data:`_IDF_CORPUS_CAP` shots.

    The current shot is excluded so its own tokens don't deflate the IDF of
    every term that happens to appear in it. Tokens are deduplicated *within*
    each document before counting — IDF counts documents, not occurrences.
    """
    df: Counter[str] = Counter()
    num_docs = 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ocr_text FROM screenshots "
            "WHERE id != ? AND ocr_text IS NOT NULL AND ocr_text != '' "
            "ORDER BY id DESC LIMIT ?",
            (shot_id, _IDF_CORPUS_CAP),
        )
        async for row in cursor:
            num_docs += 1
            seen: set[str] = set()
            for token in _tokenise(str(row["ocr_text"])):
                if not _meaningful(token):
                    continue
                if token in seen:
                    continue
                seen.add(token)
                df[token] += 1
    return num_docs, df


def _tf_by_source(
    ocr_text: str | None,
    window_title: str | None,
    app_name: str | None,
) -> tuple[Counter[str], dict[str, Source]]:
    """Build per-shot term frequency and remember which field each token came from.

    OCR wins ties on source attribution because it is the most informative
    field; ``title`` and ``app`` are only tagged when the token appears solely
    in those fields. This keeps the ``source`` label aligned with the user's
    intuition ("this hashtag came from the screenshot, not just the window
    chrome").
    """
    tf: Counter[str] = Counter()
    source: dict[str, Source] = {}

    for token in _tokenise(ocr_text):
        if not _meaningful(token):
            continue
        tf[token] += 1
        source[token] = "ocr"

    for token in _tokenise(window_title):
        if not _meaningful(token):
            continue
        tf[token] += 1
        # Only claim "title" if OCR didn't already.
        source.setdefault(token, "title")

    # App names are typically a single word (``"chrome"``, ``"VSCode"``) so we
    # tokenise to handle camel-case-stripped fields ("vscode"), then attribute.
    for token in _tokenise(app_name):
        if not _meaningful(token):
            continue
        tf[token] += 1
        source.setdefault(token, "app")

    return tf, source


def _score(tf: int, df: int, num_docs: int) -> float:
    """Classic smoothed TF-IDF: ``tf * log((N + 1) / (df + 1)) + 1``.

    The ``+1`` smoothing terms keep ``df = 0`` (a token that appears in this
    shot but no historical document) from blowing up to infinity and let
    ``num_docs = 0`` (cold-start, brand-new install) degrade gracefully to a
    pure-TF ranking.
    """
    return float(tf) * math.log((num_docs + 1) / (df + 1)) + 1.0


async def suggest_hashtags_for_shot(shot_id: int) -> SuggestResult:
    """Return the top-:data:`TOP_K` hashtag candidates for one screenshot.

    Args:
        shot_id: Primary key of the row in the ``screenshots`` table.

    Returns:
        ``{"shot_id": shot_id, "candidates": [...]}`` where each candidate
        carries ``tag`` (lowercase), ``score`` (TF-IDF, higher is better) and
        ``source`` (``"ocr"`` / ``"title"`` / ``"app"`` — the field the token
        first appeared in). When the shot is missing or has no usable text, the
        candidate list is empty rather than raising — the route layer decides
        whether that is a 404 or a 200 with an empty payload.
    """
    loaded = await _load_shot(shot_id)
    if loaded is None:
        log.info("hashtag_suggest.unknown_shot", shot_id=shot_id)
        return {"shot_id": shot_id, "candidates": []}

    ocr_text, window_title, app_name = loaded
    tf, source = _tf_by_source(ocr_text, window_title, app_name)
    if not tf:
        log.info("hashtag_suggest.no_tokens", shot_id=shot_id)
        return {"shot_id": shot_id, "candidates": []}

    num_docs, df = await _idf_corpus_document_frequencies(shot_id)

    scored: list[tuple[str, float, Source]] = []
    for token, freq in tf.items():
        scored.append((token, _score(freq, df.get(token, 0), num_docs), source[token]))
    scored.sort(key=lambda row: (-row[1], row[0]))

    top = scored[:TOP_K]
    candidates: list[Candidate] = [
        {"tag": tag, "score": round(score, 4), "source": src} for tag, score, src in top
    ]

    log.info(
        "hashtag_suggest.computed",
        shot_id=shot_id,
        unique_tokens=len(tf),
        idf_corpus_size=num_docs,
        returned=len(candidates),
    )
    return {"shot_id": shot_id, "candidates": candidates}


__all__ = [
    "STOPWORDS",
    "TOP_K",
    "Candidate",
    "SuggestResult",
    "suggest_hashtags_for_shot",
]
