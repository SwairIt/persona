"""Visual diff between two ``shot_annotation_revision`` rows (v1.46).

v1.45 introduced an immutable revision timeline for the screenshot
annotation editor (see :mod:`app.shot_annotation_history`). The UI
shipped with restore-only semantics — pick an older row, overwrite the
live state. The next obvious step is *comparison*: given two revision
ids, surface exactly which SVG primitives were added / removed between
them so a user can audit "what did I change in the last two minutes"
without restoring anything.

Design contract
---------------
* **Read-only.** Nothing in this module mutates either row. The diff is
  computed in-process from the two payloads and returned as plain
  Python dicts; the DB only sees two parametrised ``SELECT``s.
* **Tolerant regex extraction.** The annotation editor emits a small,
  well-defined dialect of SVG (``<rect>`` / ``<line>`` / ``<path>`` /
  ``<text>``), but the payloads have been round-tripped through
  ``sanitise_svg`` and the browser's ``innerHTML`` serialiser — attribute
  order is not stable, whitespace varies, ``data-selected`` may leak
  through, etc. Standing up a real DOM parser (lxml, html5lib) for a
  read-only diff is overkill and adds a heavy optional dep; instead we
  match each primitive tag with a permissive regex and compare the
  resulting *attribute set* (parsed into a sorted dict). This is the
  same approach the editor itself relies on for sanitisation.
* **Set-diff over normalised signatures.** Each extracted element is
  hashed by ``(tag, sorted(attrs))`` so reordered attributes do not
  register as a change. Text-element ``textContent`` is folded into the
  signature so two identical ``<text>`` boxes with different labels
  still count as distinct.
* **Bounding box for the overlay UI.** The HTML view renders the diff
  by drawing the original SVG and outlining changed primitives — to do
  that the consumer needs coordinates. :func:`compute_revision_diff`
  returns a best-effort ``bbox`` per element (None when geometry cannot
  be inferred from attributes alone, e.g. a bare ``<text>`` with no
  ``x``/``y``).
* **Parametrised SQL + structlog.** Mandatory per project conventions.
"""

from __future__ import annotations

import re
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.annotation_diff")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class BBox(TypedDict):
    """Axis-aligned bounding box in SVG user-space coordinates.

    All four fields are floats so a future fractional coordinate from a
    high-DPI capture does not silently truncate; the editor emits
    integer pixels today but the diff view should not assume so.
    """

    x: float
    y: float
    width: float
    height: float


class DiffElement(TypedDict):
    """One primitive that appeared in exactly one of the two revisions."""

    tag: str
    attrs_str: str
    bbox: BBox | None


class RevisionDiff(TypedDict):
    """Result of :func:`compute_revision_diff`."""

    shot_id: int
    rev_a_id: int
    rev_b_id: int
    rev_a_saved_at: str
    rev_b_saved_at: str
    rev_a_svg: str
    rev_b_svg: str
    added: list[DiffElement]
    removed: list[DiffElement]
    kept_count: int


# ---------------------------------------------------------------------------
# Tolerant SVG primitive extraction
# ---------------------------------------------------------------------------


# Captured primitives. ``<text>`` is handled separately because we want
# its inner text content as part of the signature.
_VOID_TAGS: tuple[str, ...] = ("rect", "line", "path")

# Permissive: match ``<tag ... />`` OR ``<tag ...>...</tag>``. We do not
# care about the body for void tags. Case-insensitive so a tampered
# payload with ``<Rect>`` is still classified.
_VOID_RE: dict[str, re.Pattern[str]] = {
    tag: re.compile(
        rf"<{tag}\b([^>]*?)/?>",
        re.IGNORECASE | re.DOTALL,
    )
    for tag in _VOID_TAGS
}

_TEXT_RE: re.Pattern[str] = re.compile(
    r"<text\b([^>]*?)>(.*?)</text\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Attribute splitter — handles both single and double quotes. We
# intentionally do NOT support unquoted HTML-style attribute values; the
# editor always quotes (the browser serialiser does too) and tolerating
# unquoted forms would require a real parser.
_ATTR_RE: re.Pattern[str] = re.compile(
    r"""([a-zA-Z_][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.DOTALL,
)


def _parse_attrs(attr_blob: str) -> dict[str, str]:
    """Pull a flat ``{name: value}`` map out of a raw attribute string.

    The ``data-selected`` attribute is filtered out — it is a transient
    UI marker the editor strips before save, but a payload generated
    mid-drag could still contain it. Treating it as a real attribute
    would mis-classify otherwise-identical shapes as changed.
    """
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(attr_blob):
        name = match.group(1)
        # Either the double- or single-quoted capture group matched.
        value = match.group(2) if match.group(2) is not None else match.group(3) or ""
        if name.lower() == "data-selected":
            continue
        attrs[name] = value
    return attrs


def _attrs_signature(attrs: dict[str, str]) -> str:
    """Stable string form of a primitive's attribute set.

    Attributes are sorted by name so a re-ordered serialisation of the
    same shape collapses to the same signature.
    """
    items = sorted(attrs.items())
    return ";".join(f"{name}={value}" for name, value in items)


def _coerce_float(raw: str | None) -> float | None:
    """Best-effort numeric parse — returns ``None`` on any failure.

    SVG attribute values can carry units (``px``, ``pt``) but the editor
    never emits them. We still strip a trailing alpha-suffix so a hand-
    crafted payload with ``width="10px"`` does not blow up the bbox.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    # Trim a unit suffix like ``px`` / ``pt``.
    match = re.match(r"^(-?\d+(?:\.\d+)?)", cleaned)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover — regex guarantees a number
        return None


def _bbox_for_rect(attrs: dict[str, str]) -> BBox | None:
    """Bounding box of a ``<rect>``. Requires x / y / width / height."""
    x = _coerce_float(attrs.get("x"))
    y = _coerce_float(attrs.get("y"))
    w = _coerce_float(attrs.get("width"))
    h = _coerce_float(attrs.get("height"))
    if x is None or y is None or w is None or h is None:
        return None
    return {"x": x, "y": y, "width": w, "height": h}


def _bbox_for_line(attrs: dict[str, str]) -> BBox | None:
    """Axis-aligned bbox enclosing the two endpoints of a ``<line>``."""
    x1 = _coerce_float(attrs.get("x1"))
    y1 = _coerce_float(attrs.get("y1"))
    x2 = _coerce_float(attrs.get("x2"))
    y2 = _coerce_float(attrs.get("y2"))
    if x1 is None or y1 is None or x2 is None or y2 is None:
        return None
    return {
        "x": min(x1, x2),
        "y": min(y1, y2),
        "width": abs(x2 - x1),
        "height": abs(y2 - y1),
    }


def _bbox_for_path(attrs: dict[str, str]) -> BBox | None:
    """Approximate bbox of a ``<path>`` by scanning its ``d`` numbers.

    We do not parse SVG path commands — for the editor's straight-segment
    paths the numeric coordinates alone form a tight enough bbox to
    drive an overlay outline. Returns ``None`` when ``d`` is missing or
    contains no numbers.
    """
    d = attrs.get("d")
    if not d:
        return None
    numbers = [float(m.group(0)) for m in re.finditer(r"-?\d+(?:\.\d+)?", d)]
    if len(numbers) < 2:
        return None
    xs = numbers[0::2]
    ys = numbers[1::2]
    if not xs or not ys:
        return None
    x_min = min(xs)
    y_min = min(ys)
    return {
        "x": x_min,
        "y": y_min,
        "width": max(xs) - x_min,
        "height": max(ys) - y_min,
    }


def _bbox_for_text(attrs: dict[str, str]) -> BBox | None:
    """Bbox for a ``<text>`` — point-ish, sized by ``font-size`` if any.

    SVG text positions its baseline at ``(x, y)``; the editor draws with
    ``font-size`` 28 by default. We emit a small box around the anchor
    so the overlay UI can still draw a visible rectangle around an
    added/removed label. Returns ``None`` when ``x``/``y`` are absent.
    """
    x = _coerce_float(attrs.get("x"))
    y = _coerce_float(attrs.get("y"))
    if x is None or y is None:
        return None
    size = _coerce_float(attrs.get("font-size")) or 16.0
    # Baseline is at y; the visible glyphs sit roughly above it. Inflate
    # a single-em square so the outline is clearly visible.
    return {"x": x, "y": y - size, "width": size * 4.0, "height": size * 1.2}


def _bbox_for(tag: str, attrs: dict[str, str]) -> BBox | None:
    if tag == "rect":
        return _bbox_for_rect(attrs)
    if tag == "line":
        return _bbox_for_line(attrs)
    if tag == "path":
        return _bbox_for_path(attrs)
    if tag == "text":
        return _bbox_for_text(attrs)
    return None  # pragma: no cover — only the four tags above flow in


def _extract_elements(svg_payload: str) -> list[tuple[str, str, BBox | None]]:
    """Pull every primitive out of ``svg_payload``.

    Returns a list of ``(tag, signature, bbox)`` triples. Order matches
    the appearance order in the payload, which the diff routine ignores
    for the set comparison but the consumer may use for stable display.
    """
    found: list[tuple[str, str, BBox | None]] = []
    for tag, pattern in _VOID_RE.items():
        for match in pattern.finditer(svg_payload):
            attrs = _parse_attrs(match.group(1))
            signature = _attrs_signature(attrs)
            found.append((tag, signature, _bbox_for(tag, attrs)))

    for match in _TEXT_RE.finditer(svg_payload):
        attrs = _parse_attrs(match.group(1))
        # Fold the inner text content into the signature so two
        # otherwise-identical ``<text>`` anchors with different labels
        # still register as distinct primitives.
        body = match.group(2).strip()
        attrs["__text__"] = body
        signature = _attrs_signature(attrs)
        found.append(("text", signature, _bbox_for("text", attrs)))

    return found


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def _load_revision(
    shot_id: int, revision_id: int
) -> tuple[str, str] | None:
    """Fetch ``(svg_payload, saved_at)`` for one revision, scoped to ``shot_id``.

    Scoping the SELECT to both ids ensures a caller cannot pull a
    revision belonging to a different screenshot just by guessing its
    id. Returns ``None`` if the row does not exist (or belongs to a
    different shot).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT svg_payload, saved_at
            FROM shot_annotation_revision
            WHERE id = ? AND screenshot_id = ?
            """,
            (int(revision_id), int(shot_id)),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["svg_payload"]), str(row["saved_at"])


def _diff_buckets(
    a_elements: list[tuple[str, str, BBox | None]],
    b_elements: list[tuple[str, str, BBox | None]],
) -> tuple[list[DiffElement], list[DiffElement], int]:
    """Bucket primitives into added / removed / kept.

    A primitive is "kept" when its ``(tag, signature)`` pair appears in
    both revisions at least once. We use multiset semantics so two
    identical rectangles in A and only one in B yield exactly one
    ``removed`` row, not zero.
    """
    a_counts: dict[tuple[str, str], int] = {}
    for tag, sig, _bbox in a_elements:
        key = (tag, sig)
        a_counts[key] = a_counts.get(key, 0) + 1

    b_counts: dict[tuple[str, str], int] = {}
    for tag, sig, _bbox in b_elements:
        key = (tag, sig)
        b_counts[key] = b_counts.get(key, 0) + 1

    kept = 0
    for key, count_a in a_counts.items():
        kept += min(count_a, b_counts.get(key, 0))

    # Reproduce the diff in display order: walk B for additions, A for
    # removals, decrementing the *other* side's running budget so a
    # duplicate primitive on both sides is correctly counted as "kept"
    # exactly once.
    remaining_in_a = dict(a_counts)
    added: list[DiffElement] = []
    for tag, sig, bbox in b_elements:
        key = (tag, sig)
        budget = remaining_in_a.get(key, 0)
        if budget > 0:
            remaining_in_a[key] = budget - 1
            continue
        added.append({"tag": tag, "attrs_str": sig, "bbox": bbox})

    remaining_in_b = dict(b_counts)
    removed: list[DiffElement] = []
    for tag, sig, bbox in a_elements:
        key = (tag, sig)
        budget = remaining_in_b.get(key, 0)
        if budget > 0:
            remaining_in_b[key] = budget - 1
            continue
        removed.append({"tag": tag, "attrs_str": sig, "bbox": bbox})

    return added, removed, kept


async def compute_revision_diff(
    shot_id: int,
    rev_a_id: int,
    rev_b_id: int,
) -> RevisionDiff:
    """Compute the visual diff between two revisions of one screenshot.

    Both revisions must belong to ``shot_id``; mismatches raise
    :class:`ValueError`. The function loads both payloads with a single
    SQL round-trip each, extracts the SVG primitives with the tolerant
    regex extractor above, and returns the bucketed result.

    The two payloads are returned alongside the diff so a downstream
    template can render the original A and B SVG canvases and only need
    the diff buckets to draw overlay highlights.
    """
    pair_a = await _load_revision(shot_id, rev_a_id)
    if pair_a is None:
        msg = f"revision {rev_a_id} not found for shot {shot_id}"
        raise ValueError(msg)
    pair_b = await _load_revision(shot_id, rev_b_id)
    if pair_b is None:
        msg = f"revision {rev_b_id} not found for shot {shot_id}"
        raise ValueError(msg)

    payload_a, saved_a = pair_a
    payload_b, saved_b = pair_b

    # Order by saved_at: the "earlier" payload is A regardless of which
    # id the caller passed first. This makes the added/removed labels
    # intuitive (added = appeared later, removed = was there earlier
    # and is gone now). A tie on timestamp falls back to id order so
    # the result is deterministic in tests.
    if (saved_b, rev_b_id) < (saved_a, rev_a_id):
        payload_a, payload_b = payload_b, payload_a
        saved_a, saved_b = saved_b, saved_a
        rev_a_id, rev_b_id = rev_b_id, rev_a_id

    elements_a = _extract_elements(payload_a)
    elements_b = _extract_elements(payload_b)
    added, removed, kept_count = _diff_buckets(elements_a, elements_b)

    log.info(
        "annotation_diff.compute",
        shot_id=int(shot_id),
        rev_a_id=int(rev_a_id),
        rev_b_id=int(rev_b_id),
        elements_a=len(elements_a),
        elements_b=len(elements_b),
        added=len(added),
        removed=len(removed),
        kept=kept_count,
    )

    return {
        "shot_id": int(shot_id),
        "rev_a_id": int(rev_a_id),
        "rev_b_id": int(rev_b_id),
        "rev_a_saved_at": saved_a,
        "rev_b_saved_at": saved_b,
        "rev_a_svg": payload_a,
        "rev_b_svg": payload_b,
        "added": added,
        "removed": removed,
        "kept_count": kept_count,
    }
