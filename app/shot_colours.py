"""Per-shot dominant colour palette extraction.

v1.8 feature 2/3. Companion to migration ``089_shot_colours.sql``. For
every screenshot Persona stores, this module derives a *k*-entry colour
palette — a small list of ``{hex, weight_pct}`` rows — by quantizing the
thumbnail with :func:`PIL.Image.quantize` and ranking the resulting
palette entries by pixel count.

The palette is cached in the ``shot_colour`` table keyed by the
screenshot id. A second call to :func:`compute_palette` for the same
shot is a single SELECT that returns the cached JSON unchanged — the
expensive quantize pass runs at most once per screenshot.

The companion to the per-OCR-word ``bg_hex`` / ``fg_hex`` columns
(migration ``049``) is intentional: those colours describe individual
glyphs, this palette describes the *whole frame*. The two
representations complement each other — glyph search vs. background-
canvas search — and never overlap.

PIL is sync, so :func:`compute_palette` farms the quantize + crop work
out to a worker thread via :func:`anyio.to_thread.run_sync`. The route
layer can call the coroutine directly from the FastAPI handler without
blocking the event loop, and the OCR worker calls it inside its own
``try/except`` side-channel so a quantize failure never poisons the
primary OCR pipeline.

Logger names follow the project convention (``persona.<feature>``):
the canonical logger here is ``persona.shot_colours``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypedDict

import anyio
from PIL import Image, UnidentifiedImageError

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.shot_colours")

# Upper bound on the per-call ``k`` parameter. PIL's quantize accepts up
# to 256 palette entries; keeping the public surface much smaller keeps
# the JSON payload small (one screenshot detail page never wants to
# render 200 swatches) and bounds the worker-thread CPU cost.
_K_MIN: Final[int] = 1
_K_MAX: Final[int] = 32
# Default ``k`` matches the spec ("k=5"); five is enough to express the
# dominant theme of a typical desktop screenshot (background, chrome,
# content, accent, secondary accent) without dragging in noise.
_K_DEFAULT: Final[int] = 5


class PaletteEntry(TypedDict):
    """One slot of the per-shot palette.

    * ``hex``         — ``#rrggbb`` (lowercase, leading ``#``).
    * ``weight_pct``  — Share of the quantized image attributable to
                        this palette index, in ``[0.0, 100.0]``,
                        rounded to one decimal place.
    """

    hex: str
    weight_pct: float


class _BulkResult(TypedDict):
    """Tally returned by :func:`bulk_compute`.

    * ``scanned`` — rows the SELECT returned (``<= limit``).
    * ``computed`` — rows whose palette we wrote.
    * ``skipped`` — rows we touched but could not quantize (missing
      thumbnail, missing file, PIL refused the bytes).
    """

    scanned: int
    computed: int
    skipped: int


def _normalise_k(k: int) -> int:
    """Clamp the caller's ``k`` into the supported ``[_K_MIN, _K_MAX]`` range.

    A misconfigured caller (``0``, a negative ``k``, an absurd
    ``10_000``) silently falls back to the supported range rather than
    raising — the OCR worker invokes this through a ``try/except``
    side-channel and we never want a UI side-channel to fail because a
    knob is out of range.
    """
    if k < _K_MIN:
        return _K_MIN
    if k > _K_MAX:
        return _K_MAX
    return k


def _palette_hex(palette: list[int], index: int) -> str | None:
    """Extract the ``#rrggbb`` triplet at ``index`` from a flat RGB palette.

    Mirrors :func:`app.ocr.colour_sample._palette_hex` so the two
    colour-extraction code paths emit identical hex formatting and the
    UI can mix-and-match the two data sources without a normalisation
    layer. Returns ``None`` when ``index`` falls outside the palette —
    PIL has been known to emit a count entry that refers to a missing
    palette slot on degenerate inputs (single-colour crops).
    """
    base = index * 3
    if base < 0 or base + 2 >= len(palette):
        return None
    r = palette[base] & 0xFF
    g = palette[base + 1] & 0xFF
    b = palette[base + 2] & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"


def _quantize_palette(thumb_path: Path, k: int) -> list[PaletteEntry] | None:
    """Open ``thumb_path``, quantize to ``k`` colours, return the ranked palette.

    Sync (PIL + disk IO); invoke via :func:`anyio.to_thread.run_sync`.
    Returns ``None`` when the file is missing, unreadable, or PIL
    declines it — the caller turns ``None`` into a skip rather than a
    failure tally.

    Output order is by descending pixel count so the first entry is the
    dominant colour. ``weight_pct`` sums to ``100.0`` (within floating-
    point rounding) across the returned list when the source image has
    no transparent pixels; we do not renormalise after rounding because
    the UI never displays the percentages alongside a "must sum to
    100" claim.
    """
    if not thumb_path.exists():
        log.debug("shot_colours.thumb_missing", path=str(thumb_path))
        return None
    try:
        with Image.open(thumb_path) as image:
            image.load()
            # ``quantize`` requires a single-band or RGB source; normalise
            # so palette/RGBA/L-mode thumbnails all collapse to RGB
            # cleanly. Mirrors :mod:`app.ocr.colour_sample`.
            rgb = image.convert("RGB")
            quantized = rgb.quantize(colors=k)
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        log.warning(
            "shot_colours.read_failed",
            path=str(thumb_path),
            error=str(exc),
        )
        return None

    raw_palette = quantized.getpalette()
    palette: list[int] = [int(v) for v in raw_palette] if raw_palette else []
    # ``getcolors`` on a palette image yields ``[(count, palette_index), ...]``.
    # On a quantize result with ``colors=k`` there are at most ``k``
    # entries — well under PIL's default ``maxcolors=256`` ceiling — but
    # pass ``maxcolors`` explicitly to make the intent obvious.
    raw_counts = quantized.getcolors(maxcolors=max(k, 1)) or []
    if not raw_counts:
        log.debug("shot_colours.empty_quantize", path=str(thumb_path))
        return None

    counts: list[tuple[int, int]] = []
    for entry in raw_counts:
        count_val, index_val = entry
        try:
            counts.append((int(count_val), int(index_val)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    if not counts:
        return None

    counts.sort(key=lambda item: item[0], reverse=True)
    total = sum(count for count, _ in counts)
    if total <= 0:
        return None

    out: list[PaletteEntry] = []
    for count, index in counts:
        hex_value = _palette_hex(palette, index)
        if hex_value is None:
            continue
        weight = round(count * 100.0 / total, 1)
        out.append(PaletteEntry(hex=hex_value, weight_pct=weight))
    return out or None


async def _read_cached(
    conn: aiosqlite.Connection,
    shot_id: int,
) -> list[PaletteEntry] | None:
    """Return the cached palette for ``shot_id`` or ``None`` if absent.

    Decode failures (a corrupt JSON blob written by an earlier version,
    a manual ``UPDATE`` gone wrong) collapse to ``None`` so the caller
    transparently recomputes — the cache must never be load-bearing for
    correctness.
    """
    cursor = await conn.execute(
        "SELECT palette_json FROM shot_colour WHERE shot_id = ?",
        (shot_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        decoded = json.loads(str(row["palette_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        log.warning("shot_colours.cache_decode_failed", shot_id=shot_id, error=str(exc))
        return None
    if not isinstance(decoded, list):
        return None
    out: list[PaletteEntry] = []
    for entry in decoded:
        if not isinstance(entry, dict):
            continue
        hex_value = entry.get("hex")
        weight = entry.get("weight_pct")
        if not isinstance(hex_value, str) or not isinstance(weight, int | float):
            continue
        out.append(PaletteEntry(hex=hex_value, weight_pct=float(weight)))
    return out or None


async def _read_thumb_path(
    conn: aiosqlite.Connection,
    shot_id: int,
) -> str | None:
    """Look up ``screenshots.thumbnail_path`` for ``shot_id``.

    Returns ``None`` when the screenshot row is gone (deleted in
    parallel) or the thumbnail column is ``NULL`` (smart-min-gap
    suppression — no on-disk image to quantize).
    """
    cursor = await conn.execute(
        "SELECT thumbnail_path FROM screenshots WHERE id = ?",
        (shot_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    value = row["thumbnail_path"]
    if value is None:
        return None
    return str(value)


async def _write_cache(
    conn: aiosqlite.Connection,
    shot_id: int,
    palette: list[PaletteEntry],
) -> None:
    """Persist ``palette`` for ``shot_id`` via ``INSERT OR REPLACE``.

    ``INSERT OR REPLACE`` keeps the function safe to call concurrently
    with a re-run (e.g. a backfill that overlaps with the OCR worker's
    side-channel write) — the second writer overwrites the first
    instead of raising on a primary-key conflict. Caller is responsible
    for the surrounding ``commit()``.
    """
    encoded = json.dumps(list(palette), separators=(",", ":"))
    await conn.execute(
        "INSERT OR REPLACE INTO shot_colour (shot_id, palette_json, computed_at) "
        "VALUES (?, ?, datetime('now'))",
        (shot_id, encoded),
    )


async def compute_palette(  # noqa: PLR0911 — every return is a distinct failure branch we want to log
    shot_id: int,
    k: int = _K_DEFAULT,
) -> list[PaletteEntry] | None:
    """Return the cached or freshly-computed palette for one screenshot.

    The first call for a given ``shot_id`` opens the thumbnail, runs
    :func:`PIL.Image.quantize` to ``k`` colours in a worker thread, and
    caches the resulting palette in the ``shot_colour`` table. Later
    calls bypass PIL entirely and return the cached JSON decoded back
    into the same shape.

    Returns ``None`` when:

    * the screenshot row is gone,
    * the thumbnail column is ``NULL`` (smart-min-gap suppression),
    * the thumbnail file is missing on disk,
    * PIL refuses the file bytes,
    * the quantize result was empty (degenerate image).

    Never raises — every failure path collapses to ``None`` so the OCR
    worker side-channel can call this in a bare ``try/except`` without
    risking the primary pipeline.
    """
    effective_k = _normalise_k(int(k))

    try:
        async with get_connection() as conn:
            cached = await _read_cached(conn, shot_id)
            if cached is not None:
                return cached
            thumb_str = await _read_thumb_path(conn, shot_id)
    except Exception as exc:
        log.warning(
            "shot_colours.cache_lookup_failed",
            shot_id=shot_id,
            error=str(exc),
        )
        return None

    if thumb_str is None:
        log.debug("shot_colours.thumb_unset", shot_id=shot_id)
        return None

    thumb_path = Path(thumb_str)
    try:
        palette = await anyio.to_thread.run_sync(
            _quantize_palette,
            thumb_path,
            effective_k,
        )
    except Exception as exc:
        log.warning(
            "shot_colours.quantize_failed",
            shot_id=shot_id,
            error=str(exc),
        )
        return None

    if palette is None:
        return None

    try:
        async with get_connection() as conn:
            await _write_cache(conn, shot_id, palette)
            await conn.commit()
    except Exception as exc:
        # Cache write failed but we still have a fresh palette in hand —
        # serve it from this call and let a later run re-attempt the
        # cache write. Never let a write hiccup turn a successful
        # compute into a ``None``.
        log.warning(
            "shot_colours.cache_write_failed",
            shot_id=shot_id,
            error=str(exc),
        )
        return palette

    log.info(
        "shot_colours.computed",
        shot_id=shot_id,
        k=effective_k,
        entries=len(palette),
    )
    return palette


async def _select_pending(
    conn: aiosqlite.Connection,
    limit: int,
) -> list[int]:
    """Return ids of screenshots that still lack a cached palette.

    Joins ``screenshots`` against ``shot_colour`` and filters to rows
    where the join missed (``sc.shot_id IS NULL``) and the source has a
    thumbnail to quantize. Ordered ascending so a repeated call picks
    up older shots first — a backfill against a long-lived install
    settles deterministically rather than thrashing around the newest
    rows.
    """
    cursor = await conn.execute(
        "SELECT s.id AS id FROM screenshots s "
        "LEFT JOIN shot_colour sc ON sc.shot_id = s.id "
        "WHERE sc.shot_id IS NULL "
        "AND s.thumbnail_path IS NOT NULL "
        "ORDER BY s.id "
        "LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [int(row["id"]) for row in rows]


async def bulk_compute(limit: int = 500) -> _BulkResult:
    """Compute palettes for up to ``limit`` screenshots missing one.

    Walks the first ``limit`` rows of ``screenshots`` whose ``id`` is
    not yet in ``shot_colour`` and farms each thumbnail through
    :func:`compute_palette`. Returns a tally of ``{scanned, computed,
    skipped}`` so an admin/operator route can render the result without
    a second DB hit.

    Safe to call repeatedly — each pass picks up where the previous
    one left off because rows with a cached palette drop out of the
    ``LEFT JOIN`` filter.

    ``limit`` is clamped to a sensible floor of ``1`` so a misconfigured
    caller (e.g. an admin form posting ``0``) cannot turn the call into
    a no-op that still hits SQLite.
    """
    effective_limit = max(1, int(limit))

    try:
        async with get_connection() as conn:
            pending = await _select_pending(conn, effective_limit)
    except Exception as exc:
        log.warning("shot_colours.bulk_select_failed", error=str(exc))
        return _BulkResult(scanned=0, computed=0, skipped=0)

    scanned = len(pending)
    computed = 0
    skipped = 0

    for shot_id in pending:
        result = await compute_palette(shot_id)
        if result is None:
            skipped += 1
            continue
        computed += 1

    log.info(
        "shot_colours.bulk_computed",
        scanned=scanned,
        computed=computed,
        skipped=skipped,
        limit=effective_limit,
    )
    return _BulkResult(scanned=scanned, computed=computed, skipped=skipped)
