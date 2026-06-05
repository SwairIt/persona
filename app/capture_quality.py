"""Live capture-quality knob — kv-backed JPEG/WebP encode quality (10-95).

This module owns the runtime tunable ``capture_image_quality`` (kv row),
which the thumbnail encoder reads at write time so a UI slider can move
the trade-off between bytes-on-disk and visual fidelity *without* a
restart.

Default of ``85`` is intentional even though
:class:`app.settings.config.Settings.thumbnail_quality` defaults to
``35``: the env-side default keeps existing installations on their
aggressive 25 MB/day budget, while the *runtime* slider exposes a more
forgiving "JPEG/WebP-style" range (10..95) for operators who want to
nudge sharpness up after spotting OCR drift via the
:mod:`app.quality_sampler` ledger. The encode site (see
:func:`app.storage.thumbnails.save_thumbnail`) reads the kv row first
and falls back to ``Settings.thumbnail_quality`` only when the row is
unset — so an operator who never visits ``/settings/capture-quality``
gets the exact same behaviour as before this module was added.

The estimator (:func:`estimate_size_at_quality`) re-encodes a handful
of recent thumbnails at the canonical quality bands (30/50/70/85/95)
and returns the average bytes per band. The UI plots those numbers so
the operator can see "raising 70 -> 85 costs ~3.4 KB per frame" before
committing.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Final

from PIL import Image, UnidentifiedImageError

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, list_screenshots, set_kv

log = get_logger("persona.capture_quality")

# kv row name. Kept stable for migrations and the effective-settings
# resolver; the encode site reads this exact key.
KV_KEY: Final[str] = "capture_image_quality"

# Hard bounds. 10 is the practical floor before WebP collapses to
# unreadable mush; 95 is the practical ceiling above which file-size
# explodes for negligible visual gain (WebP/JPEG curves flatten).
QUALITY_MIN: Final[int] = 10
QUALITY_MAX: Final[int] = 95

# Default when the kv row is missing. Higher than Settings.thumbnail_quality
# (35) because this knob is the operator-facing slider — its default
# represents "sane mid-range for first-time users", not the aggressive
# storage-budget default the headless install ships with.
DEFAULT_QUALITY: Final[int] = 85

# Quality bands the estimator probes. Spread across the practical
# usable range so the resulting chart shows the diminishing-returns
# elbow without overwhelming the UI with rows.
PROBE_QUALITIES: Final[tuple[int, ...]] = (30, 50, 70, 85, 95)


def _clamp(value: int) -> int:
    """Clamp ``value`` into ``[QUALITY_MIN, QUALITY_MAX]``."""
    if value < QUALITY_MIN:
        return QUALITY_MIN
    if value > QUALITY_MAX:
        return QUALITY_MAX
    return value


async def get_current_quality() -> int:
    """Return the live ``capture_image_quality`` value from kv_settings.

    Falls back to :data:`DEFAULT_QUALITY` when the row is missing or
    contains a non-integer payload (e.g. someone hand-edited the row).
    The fallback is the same default the encode site uses, so the UI
    always shows what the encoder will actually do.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, KV_KEY)

    if raw is None or str(raw).strip() == "":
        return DEFAULT_QUALITY

    try:
        return _clamp(int(float(str(raw).strip())))
    except (TypeError, ValueError):
        log.warning(
            "capture_quality.kv_parse_failed",
            raw=str(raw)[:40],
            fallback=DEFAULT_QUALITY,
        )
        return DEFAULT_QUALITY


async def set_quality(value: int) -> None:
    """Persist the slider value, clamped to ``[QUALITY_MIN, QUALITY_MAX]``.

    The clamp is applied silently — submitting ``-5`` floors to ``10``
    and submitting ``200`` ceils to ``95``. The structlog line records
    both the raw input and the stored value so an operator can spot a
    misconfigured form sending wildly out-of-range numbers.
    """
    clamped = _clamp(int(value))
    async with get_connection() as conn:
        await set_kv(conn, KV_KEY, str(clamped))
    log.info(
        "capture_quality.updated",
        raw=int(value),
        stored=clamped,
    )


def _reencode_bytes(image: Image.Image, quality: int) -> int:
    """Return the byte length when ``image`` is re-encoded at ``quality``.

    Uses an in-memory buffer so no temp files are touched. ``method=6``
    matches the production encode site (highest-compression effort) so
    the estimate is faithful to what the encoder will actually write.
    """
    buf = io.BytesIO()
    image.save(buf, format="WEBP", quality=quality, method=6)
    return buf.tell()


async def _collect_sample_paths(sample_count: int) -> list[Path]:
    """Pick recent screenshots that have a thumbnail file on disk.

    ``list_screenshots`` returns DESC by ``captured_at`` so we walk from
    newest backwards. We over-fetch by ``3x`` because some recent rows
    legitimately lack a thumbnail (smart-thumbnail skip / over-budget
    skip), and we still want roughly ``sample_count`` real samples.
    """
    over_fetch = max(sample_count * 3, sample_count + 5)
    async with get_connection() as conn:
        screenshots = await list_screenshots(conn, limit=over_fetch)

    out: list[Path] = []
    for shot in screenshots:
        if shot.thumbnail_path is None:
            continue
        path = Path(shot.thumbnail_path)
        if not path.exists():
            continue
        out.append(path)
        if len(out) >= sample_count:
            break
    return out


async def estimate_size_at_quality(sample_count: int = 20) -> dict[str, object]:
    """Re-encode recent thumbnails at each probe quality, return averages.

    Returns a dict shaped for direct JSON serialisation::

        {
            "sample_count": 17,             # how many real samples we got
            "requested": 20,                # what the caller asked for
            "bands": [
                {"quality": 30, "avg_bytes": 5824, "delta_vs_current": -3400},
                ...
            ],
            "current_quality": 85,          # the live kv value
        }

    Missing samples (empty DB, no thumbnails on disk, all files
    unreadable) collapse to an empty ``bands`` list — the caller renders
    an empty-state instead of dividing by zero. Per-file decode errors
    are logged at debug and skipped.
    """
    paths = await _collect_sample_paths(sample_count)
    current_quality = await get_current_quality()

    if not paths:
        log.info(
            "capture_quality.estimate.empty",
            requested=sample_count,
        )
        return {
            "sample_count": 0,
            "requested": sample_count,
            "bands": [],
            "current_quality": current_quality,
        }

    sums: dict[int, int] = {q: 0 for q in PROBE_QUALITIES}
    counts: dict[int, int] = {q: 0 for q in PROBE_QUALITIES}

    for path in paths:
        try:
            with Image.open(path) as img:
                img.load()
                # Detach from the file handle so the with-block can close
                # without invalidating the in-memory pixels we're about
                # to re-encode against PROBE_QUALITIES.
                pixels = img.copy()
        except (OSError, UnidentifiedImageError) as exc:
            log.debug(
                "capture_quality.sample_decode_failed",
                path=str(path),
                error=str(exc),
            )
            continue

        for quality in PROBE_QUALITIES:
            try:
                size_bytes = _reencode_bytes(pixels, quality)
            except (OSError, ValueError) as exc:
                log.debug(
                    "capture_quality.reencode_failed",
                    path=str(path),
                    quality=quality,
                    error=str(exc),
                )
                continue
            sums[quality] += size_bytes
            counts[quality] += 1

    # Pick the band whose quality matches the current setting (or the
    # nearest neighbour) as the "you are here" anchor for delta calc.
    closest = min(PROBE_QUALITIES, key=lambda q: abs(q - current_quality))
    current_avg = (
        sums[closest] // counts[closest] if counts[closest] else None
    )

    bands: list[dict[str, object]] = []
    for quality in PROBE_QUALITIES:
        if counts[quality] == 0:
            continue
        avg = sums[quality] // counts[quality]
        delta = None if current_avg is None else avg - current_avg
        bands.append(
            {
                "quality": quality,
                "avg_bytes": avg,
                "delta_vs_current": delta,
            }
        )

    sampled = max(counts.values()) if counts else 0
    log.info(
        "capture_quality.estimate.ready",
        requested=sample_count,
        sampled=sampled,
        bands=len(bands),
        current=current_quality,
    )
    return {
        "sample_count": sampled,
        "requested": sample_count,
        "bands": bands,
        "current_quality": current_quality,
    }


__all__ = [
    "DEFAULT_QUALITY",
    "KV_KEY",
    "PROBE_QUALITIES",
    "QUALITY_MAX",
    "QUALITY_MIN",
    "estimate_size_at_quality",
    "get_current_quality",
    "set_quality",
]
