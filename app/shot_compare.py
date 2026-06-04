"""Side-by-side comparison of two screenshots with per-block OCR text diff.

Distinct from :mod:`app.web.routes.diff_picker` (form to pick two IDs),
:mod:`app.web.routes.diff_slider` (single-image scrubber that stacks the
two thumbnails), and :mod:`app.ocr_diff` (textual unified diff via
``HtmlDiff``): this module produces a *structured* diff — a list of
``equal`` / ``insert`` / ``delete`` / ``replace`` blocks — that the
``shot_compare.html`` template renders as a coloured inline diff next to
the two thumbnails.

The diff is computed with :class:`difflib.SequenceMatcher` over
whitespace-split tokens so the per-block text is human-readable rather
than per-character noise.
"""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING, Final, Literal, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

if TYPE_CHECKING:
    from app.storage.models import Screenshot

log = get_logger("persona.shot_compare")

# Whitespace-only split — keeps punctuation glued to its word, which is
# good enough for the per-block "what changed" story we render. The
# template displays each block joined back with single spaces, so we
# never need to preserve original spacing exactly.
_WORD_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

# Tag string emitted by :meth:`difflib.SequenceMatcher.get_opcodes`.
DiffTag = Literal["equal", "insert", "delete", "replace"]


class ShotRow(TypedDict):
    """Subset of :class:`Screenshot` columns surfaced to the template.

    Plain ``dict`` (no pydantic) so the JSON endpoint can hand the result
    straight to :class:`fastapi.responses.JSONResponse` without a custom
    encoder for :class:`datetime`. ``captured_at`` is therefore the
    ISO-8601 string we already store in SQLite, not a parsed datetime.
    """

    id: int
    captured_at: str
    app_name: str | None
    window_title: str | None
    thumbnail_path: str | None
    ocr_text: str | None


class DiffBlock(TypedDict):
    """One contiguous run of equal / changed tokens.

    ``text_a`` and ``text_b`` are the joined-by-spaces token slices from
    the respective sides. For ``insert`` blocks ``text_a`` is the empty
    string; for ``delete`` blocks ``text_b`` is empty; for ``equal``
    both are identical; for ``replace`` both are non-empty and differ.
    """

    tag: DiffTag
    text_a: str
    text_b: str


class CompareResult(TypedDict):
    a: ShotRow
    b: ShotRow
    diff_blocks: list[DiffBlock]


async def compare_shots(shot_id_a: int, shot_id_b: int) -> CompareResult:
    """Load two screenshots, compute a word-level OCR diff, return both.

    Raises :class:`LookupError` when either id is missing from the
    database — the route layer translates that into a 404. We use a
    plain stdlib exception (not :class:`fastapi.HTTPException`) so this
    module stays import-time independent of FastAPI and is unit-testable
    without spinning up an app.
    """
    async with get_connection() as conn:
        shot_a = await get_screenshot(conn, shot_id_a)
        shot_b = await get_screenshot(conn, shot_id_b)

    if shot_a is None or shot_b is None:
        missing = [
            sid for sid, shot in ((shot_id_a, shot_a), (shot_id_b, shot_b)) if shot is None
        ]
        log.info("shot_compare.not_found", missing=missing)
        msg = f"Screenshot(s) not found: {missing}"
        raise LookupError(msg)

    diff_blocks = _diff_ocr(shot_a.ocr_text, shot_b.ocr_text)

    log.info(
        "shot_compare.computed",
        id_a=shot_id_a,
        id_b=shot_id_b,
        blocks=len(diff_blocks),
    )

    return CompareResult(
        a=_row_to_dict(shot_a),
        b=_row_to_dict(shot_b),
        diff_blocks=diff_blocks,
    )


async def find_previous_shot_id(shot_id: int) -> int | None:
    """Return the closest prior screenshot id sharing the same ``app_name``.

    Best-effort SQL: scoped to ``app_name`` because a generic "previous
    by timestamp" jump would compare unrelated apps (Slack vs IDE) and
    surface a meaningless diff. Returns ``None`` when:

    * the source shot doesn't exist,
    * the source shot has no ``app_name`` (we refuse to guess), or
    * there is no earlier capture from the same app.

    Uses a single parametrised SQL with a self-join via subquery so we
    don't pull the source row through Python just to read two columns.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id
            FROM screenshots
            WHERE app_name = (
                SELECT app_name FROM screenshots WHERE id = ?
            )
              AND app_name IS NOT NULL
              AND captured_at < (
                SELECT captured_at FROM screenshots WHERE id = ?
            )
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (shot_id, shot_id),
        )
        row = await cursor.fetchone()

    if row is None:
        log.debug("shot_compare.no_previous", shot_id=shot_id)
        return None
    return int(row["id"])


def _diff_ocr(text_a: str | None, text_b: str | None) -> list[DiffBlock]:
    """Compute word-level diff blocks between two OCR strings.

    Tokenises each side on whitespace (so punctuation stays attached to
    its word), then walks
    :meth:`difflib.SequenceMatcher.get_opcodes` to build a list of
    ``equal`` / ``insert`` / ``delete`` / ``replace`` blocks. Empty
    inputs collapse to either nothing (both empty) or a single
    insert/delete block — never a half-broken result.
    """
    tokens_a = _tokenise(text_a)
    tokens_b = _tokenise(text_b)

    matcher = difflib.SequenceMatcher(a=tokens_a, b=tokens_b, autojunk=False)
    blocks: list[DiffBlock] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        # Stdlib types ``tag`` as the Literal we already alias as
        # :data:`DiffTag`, so the assignment is direct — no cast needed.
        blocks.append(
            DiffBlock(
                tag=tag,
                text_a=" ".join(tokens_a[i1:i2]),
                text_b=" ".join(tokens_b[j1:j2]),
            ),
        )
    return blocks


def _tokenise(text: str | None) -> list[str]:
    """Split on runs of whitespace, drop empty pieces."""
    if not text:
        return []
    return [t for t in _WORD_SPLIT_RE.split(text) if t]


def _row_to_dict(shot: Screenshot) -> ShotRow:
    """Project the pydantic row down to the columns the template needs."""
    return ShotRow(
        id=shot.id,
        captured_at=shot.captured_at.isoformat(),
        app_name=shot.app_name,
        window_title=shot.window_title,
        thumbnail_path=shot.thumbnail_path,
        ocr_text=shot.ocr_text,
    )
