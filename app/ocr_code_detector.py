"""Heuristic source-code detection over screenshot OCR text.

The classifier scores how likely an OCR text is source code on a
``0.0``-``1.0`` scale, treats ``>= 0.45`` as a positive flag, and
persists the bit to ``screenshots.ocr_looks_like_code`` (added in
migration ``149_ocr_code_flag.sql``). The flag drives the dedicated
``/code-shots`` browse view in :mod:`app.web.routes.code_shots`.

The heuristic is dependency-free — pure stdlib regex. False positives on
prose are acceptable; the goal is "narrow the timeline to maybe-code
shots", not literate code search.
"""

from __future__ import annotations

import re
from typing import Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.ocr_code_detector")

_THRESHOLD: Final[float] = 0.45

_INDENT_RE = re.compile(r"^(    |\t)\S", re.MULTILINE)
_BRACE_RE = re.compile(r"[{}()\[\]]")
_SEMI_OR_COMMENT_RE = re.compile(r";|//|/\*|\*/|^\s*#", re.MULTILINE)
_CAMEL_RE = re.compile(r"\b[a-z]+(?:[A-Z][a-z]+)+\b")
_SNAKE_RE = re.compile(r"\b[a-z]+_[a-z][a-z_]*\b")

_CODE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "def",
        "class",
        "function",
        "import",
        "from",
        "return",
        "const",
        "let",
        "var",
        "public",
        "private",
        "protected",
        "void",
        "static",
        "async",
        "await",
        "if",
        "else",
        "elif",
        "while",
        "for",
        "try",
        "except",
        "catch",
        "throw",
        "raise",
        "yield",
        "lambda",
        "match",
        "case",
        "switch",
        "struct",
        "enum",
        "interface",
        "trait",
        "impl",
        "fn",
    }
)


def looks_like_code(text: str) -> tuple[bool, float]:
    """Return ``(is_code, score)`` for an OCR text.

    Returns ``(False, 0.0)`` for very short inputs; the heuristic needs a
    handful of lines before it can distinguish source from prose.
    """
    if not text or len(text) < 30:
        return False, 0.0

    score = 0.0

    if _INDENT_RE.search(text):
        score += 0.20

    brace_count = len(_BRACE_RE.findall(text))
    if brace_count >= 4:
        score += 0.15

    if _SEMI_OR_COMMENT_RE.search(text):
        score += 0.15

    lower_tokens = re.findall(r"\b\w+\b", text.lower())
    if lower_tokens:
        keyword_hits = sum(1 for tok in lower_tokens if tok in _CODE_TOKENS)
        if keyword_hits >= 2:
            score += 0.25
        elif keyword_hits == 1:
            score += 0.10

    case_hits = len(_CAMEL_RE.findall(text)) + len(_SNAKE_RE.findall(text))
    if case_hits >= 5:
        score += 0.15
    elif case_hits >= 2:
        score += 0.05

    score = min(1.0, score)
    return score >= _THRESHOLD, score


async def classify_shot(shot_id: int) -> dict[str, object]:
    """Re-classify a single screenshot row and persist the flag."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ocr_text FROM screenshots WHERE id = ?",
            (shot_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"status": "not_found", "shot_id": shot_id}
        ocr_text: str = row["ocr_text"] or ""
        is_code, score = looks_like_code(ocr_text)
        await conn.execute(
            "UPDATE screenshots SET ocr_looks_like_code = ? WHERE id = ?",
            (1 if is_code else 0, shot_id),
        )
        await conn.commit()
    log.debug(
        "ocr_code_detector.classified",
        shot_id=shot_id,
        is_code=is_code,
        score=round(score, 3),
    )
    return {
        "status": "ok",
        "shot_id": shot_id,
        "is_code": is_code,
        "score": round(score, 3),
    }


async def classify_recent(limit: int = 200) -> dict[str, object]:
    """Classify up to ``limit`` recent shots whose flag is still ``0``.

    Returns counts so the worker can log progress.
    """
    classified = 0
    flagged = 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, ocr_text FROM screenshots "
            "WHERE ocr_text IS NOT NULL "
            "  AND length(ocr_text) > 30 "
            "  AND ocr_looks_like_code = 0 "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        for row in rows:
            ocr_text: str = row["ocr_text"] or ""
            is_code, _ = looks_like_code(ocr_text)
            if is_code:
                await conn.execute(
                    "UPDATE screenshots SET ocr_looks_like_code = 1 WHERE id = ?",
                    (int(row["id"]),),
                )
                flagged += 1
            classified += 1
        await conn.commit()
    log.info(
        "ocr_code_detector.batch_done",
        classified=classified,
        flagged=flagged,
    )
    return {"classified": classified, "flagged": flagged}
