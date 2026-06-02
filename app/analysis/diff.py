"""Compare two screenshots — OCR diff + metadata delta."""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from app.dedup.phash import hamming_distance


@dataclass(frozen=True, slots=True)
class DiffResult:
    added: list[str]
    removed: list[str]
    unchanged_ratio: float
    phash_hamming: int | None


def diff_screenshots(
    *,
    left_ocr: str | None,
    right_ocr: str | None,
    left_phash: str | None = None,
    right_phash: str | None = None,
) -> DiffResult:
    """Compute a token-level diff of the OCR text plus pHash hamming distance."""
    left_tokens = _tokenise(left_ocr)
    right_tokens = _tokenise(right_ocr)

    matcher = difflib.SequenceMatcher(a=left_tokens, b=right_tokens, autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    unchanged = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            added.extend(right_tokens[j1:j2])
        if tag in {"delete", "replace"}:
            removed.extend(left_tokens[i1:i2])
        if tag == "equal":
            unchanged += i2 - i1

    total = max(len(left_tokens), len(right_tokens), 1)
    ratio = unchanged / total

    hamming: int | None = None
    if left_phash and right_phash:
        try:
            hamming = hamming_distance(left_phash, right_phash)
        except ValueError:
            hamming = None

    return DiffResult(
        added=added,
        removed=removed,
        unchanged_ratio=round(ratio, 3),
        phash_hamming=hamming,
    )


def _tokenise(text: str | None) -> list[str]:
    if not text:
        return []
    return [token for token in text.split() if token]
