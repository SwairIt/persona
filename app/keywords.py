"""Top-keywords-of-the-week — frequency analysis of OCR text + free-text notes.

A deliberately small, dependency-free counter built on :class:`collections.Counter`.
Stopwords cover common English and Russian function words plus the obvious
technical noise that bleeds into OCR (URLs, file extensions, code literals).

Results are returned as ``[{"word": str, "count": int}, ...]`` so the JSON
endpoint and Jinja template can consume them without bespoke serialisation.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Final

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.keywords")


# ---------------------------------------------------------------------------
# Stopword list — common English + Russian + OCR/web noise.
# Tokens here are matched after lowercasing and non-alphanumeric stripping.
# ---------------------------------------------------------------------------

# Cyrillic stopwords kept in a single space-separated literal so the
# "ambiguous glyph" lint (RUF001/RUF003) only fires once and is silenced
# in one place — these letters are *meant* to be Cyrillic.
_RUSSIAN_STOPWORDS_RAW: Final[str] = (
    "это что как для если или его ее её так "  # noqa: RUF001
    "уже там тут был была было были буду будет "
    "есть нет при над под без про ещё еще "
    "вот вам нам них ими ему ней том тем "
    "все всё себя себе меня тебя теперь когда "  # noqa: RUF001
    "очень может можно нужно надо будут "
    "только более менее также потом затем почему "
    "чтобы потому тогда здесь сюда туда везде "
    "никогда иногда часто редко много мало сколько "
    "однако вообще просто далее вместе всегда"
)
_RUSSIAN_STOPWORDS: Final[tuple[str, ...]] = tuple(_RUSSIAN_STOPWORDS_RAW.split())


STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        # --- English function words / fillers -------------------------------
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
        "her", "was", "one", "our", "out", "day", "get", "has", "him", "his",
        "how", "man", "new", "now", "old", "see", "two", "way", "who", "boy",
        "did", "its", "let", "put", "say", "she", "too", "use", "any", "ask",
        "big", "bad", "end", "few", "got", "lot", "off", "own", "set", "top",
        "via", "yes", "yet", "from", "this", "that", "with", "have", "they",
        "your", "will", "what", "when", "make", "like", "time", "just",
        "know", "take", "into", "year", "good", "some", "them", "than", "then",
        "look", "only", "come", "over", "also", "back", "after", "work", "well",
        "even", "want", "give", "most", "find", "tell", "very", "still", "such",
        "here", "thing", "many", "much", "more", "been", "were", "would",
        "could", "should", "about", "their", "there", "these", "those", "which",
        "where", "while", "shall", "every", "first", "other", "right", "think",
        "really", "before", "between", "because", "through", "during", "though",
        # --- Russian function words (Cyrillic, ambiguous-glyph noqa applied per-line) -
        *_RUSSIAN_STOPWORDS,
        # --- Technical / OCR noise -----------------------------------------
        "http", "https", "www", "com", "org", "net", "html", "css", "url",
        "png", "jpg", "jpeg", "gif", "svg", "pdf", "mp3", "mp4", "zip", "tar",
        "file", "files", "name", "type", "true", "false", "none", "null",
        "undefined", "var", "const", "return", "import", "export",
        "function", "class", "method", "object", "array", "string", "number",
        "boolean", "void", "self", "args", "kwargs", "param", "params",
        "value", "values", "data", "item", "items", "list", "dict", "key",
        "keys", "src", "href", "alt", "div", "span", "img", "form", "input",
        "button", "label", "head", "body", "main", "footer", "header",
        "section", "article", "aside", "table", "row", "col", "cell",
        "attr", "attrs", "node", "tree", "root", "leaf",
        "page", "pages", "site", "sites", "link", "links", "menu", "click",
        "open", "close", "save", "load", "edit", "view", "show", "hide",
        "next", "prev", "home", "filter", "sort", "submit",
    }
)


def _tokenise(text: str) -> list[str]:
    """Split ``text`` on whitespace, strip non-alphanumeric, lowercase.

    The tokeniser is deliberately Unicode-aware (``str.isalnum`` returns
    ``True`` for Cyrillic characters) so Russian and English share the same
    code path.
    """
    tokens: list[str] = []
    for raw in text.split():
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if cleaned:
            tokens.append(cleaned.lower())
    return tokens


async def top_keywords(
    days: int = 7,
    top_n: int = 30,
    min_length: int = 4,
) -> list[dict[str, int | str]]:
    """Return the ``top_n`` most frequent keywords from OCR + notes.

    Args:
        days: Look-back window in days (inclusive of "now").
        top_n: Maximum number of (word, count) pairs to return.
        min_length: Drop tokens shorter than this after cleaning.

    Returns:
        ``[{"word": str, "count": int}, ...]`` sorted by count descending.
        An empty list is returned when no text is found.
    """
    if days <= 0 or top_n <= 0 or min_length <= 0:
        log.warning(
            "keywords.invalid_params",
            days=days,
            top_n=top_n,
            min_length=min_length,
        )
        return []

    cutoff = datetime.now(UTC) - timedelta(days=days)
    cutoff_iso = iso(cutoff)

    counter: Counter[str] = Counter()
    ocr_chars = 0
    note_chars = 0

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ocr_text FROM screenshots "
            "WHERE captured_at >= ? AND ocr_text IS NOT NULL AND ocr_text != ''",
            (cutoff_iso,),
        )
        async for row in cursor:
            text = str(row["ocr_text"])
            ocr_chars += len(text)
            for token in _tokenise(text):
                if len(token) < min_length or token in STOPWORDS:
                    continue
                counter[token] += 1

        cursor = await conn.execute(
            "SELECT body FROM screenshot_notes WHERE created_at >= ?",
            (cutoff_iso,),
        )
        async for row in cursor:
            body = str(row["body"])
            note_chars += len(body)
            for token in _tokenise(body):
                if len(token) < min_length or token in STOPWORDS:
                    continue
                counter[token] += 1

    result: list[dict[str, int | str]] = [
        {"word": word, "count": count} for word, count in counter.most_common(top_n)
    ]

    log.info(
        "keywords.computed",
        days=days,
        top_n=top_n,
        min_length=min_length,
        unique_tokens=len(counter),
        ocr_chars=ocr_chars,
        note_chars=note_chars,
        returned=len(result),
    )
    return result
