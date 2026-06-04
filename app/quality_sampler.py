"""Offline capture-quality A/B logger.

The user routinely tunes ``thumbnail_quality`` (default 35) and
``thumbnail_max_width`` (default 640) to keep the on-disk footprint
inside the 25 MB/day budget. Pushing the knobs too far can degrade OCR
readability without producing any loud failure mode — characters still
get extracted, they just get extracted with more mistakes. This module
is the small ledger that lets the operator notice that drift.

For the last ``N`` screenshots that already have a thumbnail on disk and
non-NULL ``ocr_text``, :func:`sample_recent` recomputes three cheap
readability proxies plus the raw file size:

* **Sharpness** — variance of a 3x3 Laplacian applied to the grayscale
  thumbnail. Higher = crisper edges, lower = blurry / smeared. Computed
  via NumPy when installed; ``None`` (graceful skip) otherwise so a
  bare-bones install still records the row and just leaves the column
  empty.
* **OCR character count** — ``len(ocr_text)``. A heuristic for how much
  the OCR engine actually managed to lift off the frame. Not a direct
  quality signal on its own (an empty desktop is legitimately blank),
  but useful when aggregated by band.
* **pHash bit-entropy** — popcount of the stored perceptual hash. Edge
  cases (a uniform colour fill) collapse the hash and surface as a low
  popcount; a busy frame fills more bits. Cheap, deterministic, and
  free at sample time because the hash is already in the row.
* **File size** — the actual bytes the thumbnail occupies. The whole
  reason the operator is tuning the knobs in the first place.

Each row is keyed by the source ``screenshot_id`` with
``UNIQUE(screenshot_id)`` on the migration side, so the sampler uses
``INSERT OR IGNORE`` and a re-run is cheap & idempotent: shots already
sampled stay untouched, only fresh ones get a row.

:func:`aggregate_by_band` then ``GROUP BY (quality_used, width_used)``
and surfaces the averages the operator wants to read: "at q=35/640px
your average sharpness is X, at q=45/900px it was Y last week".

PIL + NumPy are imported lazily / conditionally so a minimal install
without the optional thumbnail-processing stack still gets a usable
module: the sampler simply records ``sharpness=NULL`` and the
aggregation contract still holds (``AVG()`` skips NULLs naturally).

Logger name: ``persona.quality_sampler`` per project convention.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.quality_sampler")

# Public defaults. ``sample_recent`` accepts a caller-supplied ``limit``
# but the route handler also passes ``50`` explicitly so the two stay
# in sync if either default ever drifts.
_DEFAULT_LIMIT: Final[int] = 50

# Hard cap on the per-call limit. The sampler loads each thumbnail off
# disk and runs a NumPy Laplacian per row — well-bounded but not free.
# Capping the limit keeps a misclick on the UI button from spawning a
# multi-minute background task on a busy machine.
_MAX_LIMIT: Final[int] = 500


def _clamp_limit(limit: int) -> int:
    """Clamp ``limit`` into ``[1, _MAX_LIMIT]``.

    A caller-supplied ``0`` or negative limit collapses to ``1`` rather
    than silently no-op'ing — a "sample nothing" call is almost
    certainly a bug at the call site, so we still process one row and
    let the log line surface the misuse.
    """
    if limit < 1:
        return 1
    if limit > _MAX_LIMIT:
        return _MAX_LIMIT
    return limit


def _laplacian_variance(thumb_path: Path) -> float | None:
    """Return the variance of a 3x3 Laplacian over the grayscale thumbnail.

    The Laplacian kernel ``[[0, 1, 0], [1, -4, 1], [0, 1, 0]]`` is the
    classic discrete second derivative used in the "Pech-style"
    blur-detection literature: higher variance == sharper edges. We
    apply it via a single SciPy-free NumPy convolution by summing four
    shifted slices and the centre tap, which is fast and avoids any
    dependency on ``scipy.signal``.

    Returns ``None`` when either NumPy or PIL is missing, the file is
    absent, or PIL refuses the bytes. The caller turns ``None`` into a
    NULL ``sharpness`` column rather than a skip, so we still record the
    surrounding metadata (file size, OCR length, pHash entropy) for the
    aggregation.
    """
    # ``importlib.import_module`` keeps the optional deps off the
    # module-level import graph — a minimal install without numpy / PIL
    # still imports :mod:`app.quality_sampler` cleanly and the sampler
    # just records ``sharpness=NULL``. We catch ``ImportError`` rather
    # than the narrower ``ModuleNotFoundError`` because PIL can also
    # raise an ``ImportError`` on broken builds (missing system libs).
    try:
        np = importlib.import_module("numpy")
        pil_image_module = importlib.import_module("PIL.Image")
    except ImportError as exc:
        log.debug("quality_sampler.numpy_or_pil_missing", error=str(exc))
        return None

    image_open = pil_image_module.open
    unidentified_image_error = getattr(
        pil_image_module, "UnidentifiedImageError", OSError
    )

    if not thumb_path.exists():
        log.debug("quality_sampler.thumb_missing", path=str(thumb_path))
        return None

    try:
        with image_open(thumb_path) as image:
            image.load()
            grayscale = image.convert("L")
            arr = np.asarray(grayscale, dtype=np.float32)
    except (OSError, unidentified_image_error, ValueError) as exc:
        log.warning(
            "quality_sampler.read_failed",
            path=str(thumb_path),
            error=str(exc),
        )
        return None

    if arr.ndim != 2 or arr.shape[0] < 3 or arr.shape[1] < 3:
        # Degenerate single-row / single-column thumbnails would make
        # the slice arithmetic below collapse to empty arrays; the
        # variance would then be NaN. Skip gracefully.
        return None

    # Manual 3x3 Laplacian: four neighbours minus 4x centre. We restrict
    # the result to the inner region (drop the 1-pixel border) so we
    # never index out of bounds.
    centre = arr[1:-1, 1:-1]
    top = arr[0:-2, 1:-1]
    bottom = arr[2:, 1:-1]
    left = arr[1:-1, 0:-2]
    right = arr[1:-1, 2:]
    laplacian = top + bottom + left + right - 4.0 * centre

    variance = float(laplacian.var())
    return variance


def _phash_entropy_bits(phash: str | None) -> float | None:
    """Return the popcount of a hex ``phash`` string as a float.

    The popcount of the binary expansion of the perceptual hash is a
    cheap, deterministic proxy for "how much structure does this frame
    carry": a uniform-colour desktop collapses to a tiny popcount, a
    busy editor frame fills most of the bits.

    Returns ``None`` for a ``NULL`` / empty / unparseable hash so the
    column stores NULL instead of a misleading zero.
    """
    if not phash:
        return None
    try:
        value = int(phash, 16)
    except ValueError:
        log.warning("quality_sampler.bad_phash", phash=phash)
        return None
    return float(bin(value).count("1"))


async def _select_recent_candidates(
    conn: aiosqlite.Connection,
    limit: int,
) -> list[dict[str, Any]]:
    """Pick the last ``limit`` screenshots eligible for a sample row.

    Eligibility filter mirrors the spec exactly:

    * ``thumbnail_path`` non-null (we cannot measure sharpness without
      the file on disk);
    * ``ocr_text`` non-null (a NULL OCR result is itself an outcome — we
      want to measure shots the OCR pipeline actually ran on).

    We deliberately do NOT exclude shots already present in
    ``quality_sample``: the route caller wants "the last N", and the
    UPSERT below skips duplicates without spending a Laplacian pass on
    them. Anti-joining here instead would bias the recency window
    toward older shots whenever the sampler runs frequently.
    """
    cursor = await conn.execute(
        "SELECT id, thumbnail_path, ocr_text, phash "
        "FROM screenshots "
        "WHERE thumbnail_path IS NOT NULL "
        "  AND ocr_text IS NOT NULL "
        "ORDER BY id DESC "
        "LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "thumbnail_path": str(row["thumbnail_path"]),
            "ocr_text": str(row["ocr_text"]) if row["ocr_text"] is not None else "",
            "phash": (str(row["phash"]) if row["phash"] is not None else None),
        }
        for row in rows
    ]


async def _insert_sample(
    conn: aiosqlite.Connection,
    *,
    screenshot_id: int,
    quality_used: int,
    width_used: int,
    sharpness: float | None,
    ocr_chars: int,
    file_size_bytes: int | None,
    phash_entropy_bits: float | None,
) -> bool:
    """Insert a row, returning ``True`` when a row was actually added.

    ``INSERT OR IGNORE`` collides on the ``UNIQUE(screenshot_id)``
    constraint, so a repeat run for the same shot is a no-op. We surface
    that via ``rowcount`` so the caller can build an accurate "added /
    skipped" tally for the log line.
    """
    cursor = await conn.execute(
        "INSERT OR IGNORE INTO quality_sample ("
        "  screenshot_id, quality_used, width_used, "
        "  sharpness, ocr_chars, file_size_bytes, phash_entropy_bits"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            screenshot_id,
            quality_used,
            width_used,
            sharpness,
            ocr_chars,
            file_size_bytes,
            phash_entropy_bits,
        ),
    )
    return (cursor.rowcount or 0) > 0


async def sample_recent(limit: int = _DEFAULT_LIMIT) -> dict[str, int]:
    """Sample the last ``limit`` eligible screenshots into ``quality_sample``.

    Returns a tally dict with three integer counters:

    * ``scanned``  — rows the SELECT returned (``<= limit``);
    * ``inserted`` — rows the UPSERT actually wrote (excludes duplicates);
    * ``skipped``  — rows touched but not written (already sampled).

    Errors per shot are logged at WARNING and do not abort the batch —
    a single unreadable thumbnail must not poison the whole sample.

    The current ``thumbnail_quality`` / ``thumbnail_max_width`` settings
    are stamped onto every row as the "band" the sample represents.
    Re-running after the operator twiddles the knobs naturally writes
    rows under the new band for any *new* shots; existing rows are kept
    under their original band so the historical comparison stays clean.
    """
    settings = get_settings()
    quality_used = int(settings.thumbnail_quality)
    width_used = int(settings.thumbnail_max_width)

    bounded = _clamp_limit(int(limit))
    scanned = 0
    inserted = 0
    skipped = 0

    async with get_connection() as conn:
        rows = await _select_recent_candidates(conn, bounded)
        scanned = len(rows)
        for row in rows:
            thumb_path = Path(row["thumbnail_path"])
            sharpness = _laplacian_variance(thumb_path)
            ocr_chars = len(row["ocr_text"])
            phash_entropy = _phash_entropy_bits(row["phash"])

            file_size_bytes: int | None
            try:
                file_size_bytes = int(thumb_path.stat().st_size)
            except OSError as exc:
                log.warning(
                    "quality_sampler.stat_failed",
                    path=str(thumb_path),
                    error=str(exc),
                )
                file_size_bytes = None

            added = await _insert_sample(
                conn,
                screenshot_id=row["id"],
                quality_used=quality_used,
                width_used=width_used,
                sharpness=sharpness,
                ocr_chars=ocr_chars,
                file_size_bytes=file_size_bytes,
                phash_entropy_bits=phash_entropy,
            )
            if added:
                inserted += 1
            else:
                skipped += 1
        await conn.commit()

    log.info(
        "quality_sampler.sampled",
        scanned=scanned,
        inserted=inserted,
        skipped=skipped,
        quality_used=quality_used,
        width_used=width_used,
    )
    return {
        "scanned": scanned,
        "inserted": inserted,
        "skipped": skipped,
    }


async def aggregate_by_band() -> list[dict[str, Any]]:
    """Return per-(quality, width) band averages for the lab dashboard.

    Each row carries:

    * ``quality_used`` / ``width_used`` — the band coordinate;
    * ``avg_sharpness`` — mean Laplacian variance (``None`` when every
      row in the band lacks NumPy);
    * ``avg_ocr_chars`` — mean ``len(ocr_text)``;
    * ``avg_file_size_bytes`` — mean on-disk size of the thumbnail;
    * ``sample_count`` — rows aggregated into this band.

    Bands are ordered by quality then width descending so the freshest
    / most-aggressive configurations land at the top of the table.
    ``AVG()`` skips NULLs by SQLite convention, so a band that only has
    rows without sharpness data returns ``None`` for ``avg_sharpness``
    rather than ``0.0``.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT "
            "  quality_used, "
            "  width_used, "
            "  AVG(sharpness)         AS avg_sharpness, "
            "  AVG(ocr_chars)         AS avg_ocr_chars, "
            "  AVG(file_size_bytes)   AS avg_file_size_bytes, "
            "  AVG(phash_entropy_bits) AS avg_phash_entropy_bits, "
            "  COUNT(*)               AS sample_count "
            "FROM quality_sample "
            "GROUP BY quality_used, width_used "
            "ORDER BY quality_used DESC, width_used DESC"
        )
        raw_rows = await cursor.fetchall()

    out: list[dict[str, Any]] = []
    for row in raw_rows:
        avg_sharpness = row["avg_sharpness"]
        avg_ocr_chars = row["avg_ocr_chars"]
        avg_file_size = row["avg_file_size_bytes"]
        avg_phash = row["avg_phash_entropy_bits"]
        out.append(
            {
                "quality_used": int(row["quality_used"]),
                "width_used": int(row["width_used"]),
                "avg_sharpness": (
                    float(avg_sharpness) if avg_sharpness is not None else None
                ),
                "avg_ocr_chars": (
                    float(avg_ocr_chars) if avg_ocr_chars is not None else None
                ),
                "avg_file_size_bytes": (
                    float(avg_file_size) if avg_file_size is not None else None
                ),
                "avg_phash_entropy_bits": (
                    float(avg_phash) if avg_phash is not None else None
                ),
                "sample_count": int(row["sample_count"]),
            }
        )
    return out
