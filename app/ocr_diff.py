"""Textual diff between two OCR snippets — unified text + side-by-side HTML.

Persona v0.34 feature 2/3.

Pure stdlib: uses :mod:`difflib` for both the unified diff (line-oriented,
human-readable) and the HTML rendering (table with ``.diff_add`` /
``.diff_sub`` classes that the template styles green / red).

This module is intentionally sync — diff math is CPU-bound and tiny.
The async route in :mod:`app.web.routes.ocr_diff` calls this helper after
pulling ``ocr_text`` from the database.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Final

from app.logging_setup import get_logger

log = get_logger("persona.ocr_diff")

# Split on runs of whitespace AND punctuation so we get word-level granularity
# rather than line-level. Keeps actual word characters (letters, digits,
# underscores) plus accented chars together as a single token.
_WORD_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\s+|(?=[^\w\s])|(?<=[^\w\s])")

# Cap inputs to keep HtmlDiff from generating multi-megabyte tables when the
# user accidentally feeds enormous OCR blobs. Lines beyond the cap are
# truncated with a trailing marker so the page still renders.
_MAX_LINES: Final[int] = 2000


@dataclass(frozen=True, slots=True)
class OcrDiffResult:
    """Result of comparing two OCR text blobs.

    Attributes:
        unified: List of unified-diff lines (``--- a``, ``+++ b``,
            ``@@ ... @@``, ``-old``, ``+new`` …). Empty when the inputs
            are identical.
        html: A full ``<table class="diff">…</table>`` string ready to
            be dropped into a Jinja template with ``| safe``. Uses
            ``.diff_add`` (insertions) and ``.diff_sub`` (deletions)
            classes which the page styles green / red.
        identical: ``True`` when both inputs are byte-equal after
            normalisation — lets the template show a friendly notice
            instead of an empty table.
    """

    unified: list[str]
    html: str
    identical: bool


def ocr_diff(
    text_a: str | None,
    text_b: str | None,
    *,
    label_a: str = "A",
    label_b: str = "B",
) -> OcrDiffResult:
    """Compute a textual diff between two OCR snippets.

    Both ``text_a`` and ``text_b`` may be ``None`` — treated as empty
    strings. The labels show up in the unified-diff header and in the
    HtmlDiff column titles.

    Returns an :class:`OcrDiffResult` with both representations so the
    route can render whichever the template asks for without recomputing.
    """
    lines_a = _split_lines(text_a)
    lines_b = _split_lines(text_b)

    identical = lines_a == lines_b

    unified = list(
        difflib.unified_diff(
            lines_a,
            lines_b,
            fromfile=label_a,
            tofile=label_b,
            lineterm="",
        ),
    )

    # ``HtmlDiff`` does its own tokenisation via ``charjunk`` — we feed
    # word-tokenised "lines" so the intra-line highlight tracks word
    # boundaries (matches the task's "tokenise on whitespace + punctuation"
    # requirement). Joining with a single space keeps the visual layout
    # readable while still being a faithful representation of the source.
    word_lines_a = [_tokenise_for_html(line) for line in lines_a]
    word_lines_b = [_tokenise_for_html(line) for line in lines_b]

    html = difflib.HtmlDiff(wrapcolumn=80).make_table(
        word_lines_a,
        word_lines_b,
        fromdesc=label_a,
        todesc=label_b,
        context=False,
        numlines=2,
    )

    log.info(
        "ocr_diff.computed",
        len_a=len(lines_a),
        len_b=len(lines_b),
        identical=identical,
        unified_lines=len(unified),
    )

    return OcrDiffResult(unified=unified, html=html, identical=identical)


def _split_lines(text: str | None) -> list[str]:
    """Normalise input to a capped list of lines.

    ``splitlines()`` strips trailing newlines for us — :func:`difflib.unified_diff`
    handles that fine when we pass ``lineterm=""``. The cap protects against
    pathological OCR output (e.g. a screenshot of a 10k-line log file).
    """
    if not text:
        return []
    lines = text.splitlines()
    if len(lines) > _MAX_LINES:
        truncated = lines[:_MAX_LINES]
        truncated.append(f"… (truncated; {len(lines) - _MAX_LINES} more lines)")
        return truncated
    return lines


def _tokenise_for_html(line: str) -> str:
    """Re-join a line so HtmlDiff highlights word boundaries cleanly.

    HtmlDiff highlights *within* a line by char. Pre-splitting on the
    word/punctuation regex and re-joining with single spaces gives the
    highlighter natural break points, which renders as word-level diff
    rather than the default character-level noise.
    """
    if not line:
        return ""
    tokens = [t for t in _WORD_SPLIT_RE.split(line) if t and not t.isspace()]
    return " ".join(tokens) if tokens else line
