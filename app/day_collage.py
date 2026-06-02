"""Per-day collage PNG — auto-generated 4xN grid of thumbnails for sharing.

Builds a single shareable PNG poster of the day's top screenshots. The output
is intentionally square-cell (one tile per shot, letterboxed to preserve the
source aspect ratio) so the result feels like a clean contact-sheet rather
than a stretched mosaic.

Ranking policy:

* If the day has any signal-bearing frames (``tier='pinned'`` or rows in the
  ``favourite`` table), they are picked first — pinned outranks favourited,
  recency breaks ties. This mirrors the weighting used by
  :mod:`app.shot_of_week` so the "top" picks feel consistent across exports.
* Once the pinned/favourite pool is exhausted, the remainder is filled by
  most-recent ``captured_at`` until ``max_shots`` is reached.

Heavy work — ``Image.open`` plus the per-tile fit-letterbox-paste-save loop —
runs inside :func:`anyio.to_thread.run_sync` so the calling coroutine never
blocks the event loop on disk IO or pixel arithmetic.

The output PNG is written *opaquely* (RGB, ``#0b1220`` background) so the
file works as a Twitter/Telegram preview without an alpha channel surprise.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, TypedDict

import anyio
from PIL import Image

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.collage")

# Opaque dark backdrop for letterboxed tiles. Matches the dashboard's
# ``--bg-canvas`` token so a collage screenshot drops cleanly into the UI.
_BACKDROP_RGB: Final[tuple[int, int, int]] = (11, 18, 32)

# PIL's high-quality downscaler. Pinned explicitly so a future Pillow
# version that swaps the default cannot silently degrade collage sharpness.
_RESAMPLE = Image.Resampling.LANCZOS


class DayCollageResult(TypedDict):
    """Return payload for :func:`build_day_collage`."""

    status: str
    path: str | None
    tile_size: int
    cols: int
    rows: int
    shots_used: int
    size_bytes: int


def _parse_day_iso(day_iso: str) -> date:
    """Parse ``YYYY-MM-DD`` into a :class:`date` or raise :class:`ValueError`."""
    return datetime.strptime(day_iso, "%Y-%m-%d").date()


async def _load_day_shots(target: date, max_shots: int) -> list[dict[str, Any]]:
    """Return up to ``max_shots`` rows for ``target``, ranked pinned/fav/recent.

    Selection is done in a single query so SQLite handles the ordering — far
    cheaper than fetching the full day and sorting in Python. The ranking
    expression is intentionally identical in spirit to
    :mod:`app.shot_of_week` to keep "top picks" semantics consistent across
    the day/week/collage trio.
    """
    start = datetime.combine(target, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1)

    # ``rank_score`` ladder:
    #   pinned       → 2
    #   favourited   → 1
    #   neither      → 0
    # then most-recent ``captured_at`` wins the tiebreak.
    query = """
        SELECT
            s.id,
            s.captured_at,
            s.thumbnail_path,
            (
                CASE WHEN s.tier = 'pinned' THEN 2
                     WHEN f.screenshot_id IS NOT NULL THEN 1
                     ELSE 0
                END
            ) AS rank_score
        FROM screenshots AS s
        LEFT JOIN favourite AS f ON f.screenshot_id = s.id
        WHERE s.captured_at >= ? AND s.captured_at < ?
          AND s.thumbnail_path IS NOT NULL
        ORDER BY rank_score DESC, s.captured_at DESC
        LIMIT ?
    """

    async with get_connection() as conn:
        cursor = await conn.execute(query, (iso(start), iso(end), max_shots))
        rows = await cursor.fetchall()

    return [
        {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]),
            "thumbnail_path": str(row["thumbnail_path"]),
            "rank_score": int(row["rank_score"]),
        }
        for row in rows
    ]


def _resolve_thumbnail(raw: str) -> Path | None:
    """Return a readable filesystem path for the stored ``thumbnail_path``.

    Mirrors :func:`app.pdf_export._resolve_thumbnail`: try the raw value
    first (absolute paths are the production default) and fall back to the
    cwd-relative form for older rows.
    """
    candidate = Path(raw)
    if candidate.is_file():
        return candidate
    if not candidate.is_absolute():
        rooted = Path.cwd() / candidate
        if rooted.is_file():
            return rooted
    return None


def _fit_letterbox(src: Image.Image, tile_size: int) -> Image.Image:
    """Scale ``src`` into a ``tile_size`` square, preserving aspect ratio.

    The source is downscaled (never upscaled past its native resolution) to
    fit inside the tile, then pasted onto an opaque backdrop centred at the
    tile centre. Returns a fresh ``RGB`` image of exactly ``tile_size`` per
    side — the caller can paste it directly without further bookkeeping.
    """
    src_w, src_h = src.size
    if src_w <= 0 or src_h <= 0:
        # Defensive: a corrupt thumbnail should yield a blank tile rather
        # than crash the whole collage.
        return Image.new("RGB", (tile_size, tile_size), _BACKDROP_RGB)

    scale = min(tile_size / src_w, tile_size / src_h, 1.0)
    target_w = max(1, round(src_w * scale))
    target_h = max(1, round(src_h * scale))

    resized = src.resize((target_w, target_h), _RESAMPLE)
    if resized.mode != "RGB":
        resized = resized.convert("RGB")

    tile = Image.new("RGB", (tile_size, tile_size), _BACKDROP_RGB)
    offset_x = (tile_size - target_w) // 2
    offset_y = (tile_size - target_h) // 2
    tile.paste(resized, (offset_x, offset_y))
    return tile


def _render_collage(
    thumbnail_paths: list[Path],
    output_path: Path,
    *,
    cols: int,
    tile_size: int,
) -> tuple[int, int, int]:
    """Synchronous worker — composes the grid and writes the PNG.

    Returns ``(rows, shots_used, size_bytes)``. Invoked via
    :func:`anyio.to_thread.run_sync` because every operation here (PIL
    open/resize/paste plus the final ``save``) is blocking.

    A thumbnail that fails to open (corrupt file, missing on disk between
    the DB query and the read) is *skipped* rather than aborting the export
    — the missing tile becomes a backdrop-only square so the grid stays
    rectangular. The skip count is folded into ``shots_used`` so callers
    can detect a partial render without parsing logs.
    """
    n = len(thumbnail_paths)
    rows = max(1, math.ceil(n / cols))
    canvas_w = cols * tile_size
    canvas_h = rows * tile_size

    canvas = Image.new("RGB", (canvas_w, canvas_h), _BACKDROP_RGB)
    shots_used = 0

    for index, thumb_path in enumerate(thumbnail_paths):
        try:
            with Image.open(thumb_path) as src:
                src.load()
                tile = _fit_letterbox(src, tile_size)
        except (OSError, ValueError) as exc:
            # OSError covers "file vanished" and PIL's UnidentifiedImageError
            # (which subclasses OSError); ValueError covers truncated/bad
            # mode files. Either way, log and leave the cell blank.
            log.warning(
                "collage.thumbnail_unreadable",
                path=str(thumb_path),
                error=str(exc),
            )
            continue

        col_idx = index % cols
        row_idx = index // cols
        canvas.paste(tile, (col_idx * tile_size, row_idx * tile_size))
        shots_used += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    size_bytes = output_path.stat().st_size
    return rows, shots_used, size_bytes


async def build_day_collage(
    day_iso: str,
    output_path: Path | str,
    *,
    cols: int = 4,
    max_shots: int = 24,
    tile_size: int = 320,
) -> DayCollageResult:
    """Build a per-day collage PNG and return its summary.

    Args:
        day_iso: ``YYYY-MM-DD``. Anything else returns ``status="bad_date"``.
        output_path: Where to write the PNG. Parent directories are created.
        cols: Grid width. Must be >= 1.
        max_shots: Hard cap on tiles. Must be >= 1.
        tile_size: Edge length of one square tile in pixels. Must be >= 1.

    Returns:
        :class:`DayCollageResult`. ``status`` is ``"ok"`` on success,
        ``"empty"`` when the day has no thumbnailed screenshots, or
        ``"bad_date"`` / ``"bad_args"`` for input validation failures.
        ``shots_used`` may be lower than the matched row count when one
        or more thumbnails failed to open — the grid layout still matches
        the request (rows = ceil(matched / cols)) so the result is
        deterministic for a given input set.
    """
    if cols < 1 or max_shots < 1 or tile_size < 1:
        log.warning(
            "collage.bad_args",
            day=day_iso,
            cols=cols,
            max_shots=max_shots,
            tile_size=tile_size,
        )
        return DayCollageResult(
            status="bad_args",
            path=None,
            tile_size=tile_size,
            cols=cols,
            rows=0,
            shots_used=0,
            size_bytes=0,
        )

    try:
        target = _parse_day_iso(day_iso)
    except ValueError:
        log.warning("collage.bad_date", day=day_iso)
        return DayCollageResult(
            status="bad_date",
            path=None,
            tile_size=tile_size,
            cols=cols,
            rows=0,
            shots_used=0,
            size_bytes=0,
        )

    rows_data = await _load_day_shots(target, max_shots)
    if not rows_data:
        log.info("collage.empty", day=target.isoformat())
        return DayCollageResult(
            status="empty",
            path=None,
            tile_size=tile_size,
            cols=cols,
            rows=0,
            shots_used=0,
            size_bytes=0,
        )

    # Resolve every thumbnail up front: a row whose ``thumbnail_path``
    # cannot be found on disk is dropped from the layout entirely (rather
    # than producing a blank tile) so the grid stays compact.
    resolved: list[Path] = []
    for row in rows_data:
        candidate = _resolve_thumbnail(row["thumbnail_path"])
        if candidate is not None:
            resolved.append(candidate)
        else:
            log.warning(
                "collage.thumbnail_missing",
                screenshot_id=row["id"],
                path=row["thumbnail_path"],
            )

    if not resolved:
        log.info("collage.no_resolved_thumbs", day=target.isoformat())
        return DayCollageResult(
            status="empty",
            path=None,
            tile_size=tile_size,
            cols=cols,
            rows=0,
            shots_used=0,
            size_bytes=0,
        )

    out_path = Path(output_path)

    rendered_rows, shots_used, size_bytes = await anyio.to_thread.run_sync(
        lambda: _render_collage(
            resolved,
            out_path,
            cols=cols,
            tile_size=tile_size,
        )
    )

    log.info(
        "collage.built",
        day=target.isoformat(),
        path=str(out_path),
        cols=cols,
        rows=rendered_rows,
        tile_size=tile_size,
        shots_matched=len(rows_data),
        shots_resolved=len(resolved),
        shots_used=shots_used,
        size_bytes=size_bytes,
    )

    return DayCollageResult(
        status="ok",
        path=str(out_path),
        tile_size=tile_size,
        cols=cols,
        rows=rendered_rows,
        shots_used=shots_used,
        size_bytes=size_bytes,
    )


__all__ = ["DayCollageResult", "build_day_collage"]
