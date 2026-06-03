"""Background OCR worker — drains screenshots with pending OCR status."""

from __future__ import annotations

import asyncio
from pathlib import Path

import anyio
from PIL import Image

from app.image_blur import blur_sensitive_regions
from app.logging_setup import get_logger
from app.ocr import OCRNotAvailable, extract_text, is_available, redact
from app.ocr.colour_sample import sample_colours
from app.ocr.languages import refresh_ocr_lang_string
from app.ocr_phrase_tags import apply_phrase_rules
from app.redaction import apply_redaction
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.ocr_skip import is_skipped
from app.storage.regex_rules import apply_rules_to_ocr
from app.storage.repository import (
    list_pending_ocr,
    mark_pending_ocr_as_skipped,
    update_screenshot_ocr,
)
from app.storage.tags import create_tag, tag_screenshot
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.ocr_worker")
colour_log = get_logger("persona.ocr.colour")

POLL_INTERVAL_SECONDS = 2.0
BATCH_SIZE = 5
COLOUR_SAMPLE_CAP = 200
"""Per-shot upper bound on how many ocr_word rows get colour-sampled.

Quantizing every word on a dense desktop screenshot (1000+ words) would
dominate the worker's CPU budget for a UI side-channel. The cap keeps
the cost bounded; rows beyond it stay ``NULL`` and search code already
tolerates that (see ``049_ocr_word_colours.sql``).
"""

_colour_columns_present: bool | None = None
"""Module-level cache for the migration-049 column probe.

``None`` until the first probe lands; set to ``True`` when both
``bg_hex`` and ``fg_hex`` exist on ``ocr_word``, ``False`` if the
migration has not run (e.g. an older deployment that skipped 049).
Probed once and reused — the schema doesn't change at runtime.
"""


async def run_ocr_worker(controller: CaptureController | None = None) -> None:
    """Drain pending OCR jobs while the controller is alive."""
    ctrl = controller or get_controller()
    settings = get_settings()

    if not settings.ocr_enabled or not is_available(settings.tesseract_path):
        async with get_connection() as conn:
            skipped = await mark_pending_ocr_as_skipped(conn)
        log.info(
            "ocr_worker.disabled",
            ocr_enabled=settings.ocr_enabled,
            tesseract_available=is_available(settings.tesseract_path),
            skipped=skipped,
        )
        await ctrl.stop_event.wait()
        return

    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE screenshots SET ocr_status = 'pending' "
            "WHERE ocr_status = 'skipped' AND thumbnail_path IS NOT NULL"
        )
        await conn.commit()
        backfilled = cursor.rowcount or 0
    if backfilled:
        log.info("ocr_worker.backfill", count=backfilled)

    log.info("ocr_worker.started", tesseract_path=str(settings.tesseract_path))

    while not ctrl.stop_event.is_set():
        await beat("ocr-worker")
        try:
            await _drain_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("ocr_worker.iteration_failed", error=str(exc))

        try:
            await asyncio.wait_for(ctrl.stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


async def _drain_once() -> None:
    settings = get_settings()
    async with get_connection() as conn:
        pending = await list_pending_ocr(conn, limit=BATCH_SIZE)
    if not pending:
        return

    configured_langs = await _resolve_langs(settings.tesseract_langs)

    for shot in pending:
        if shot.thumbnail_path is None:
            async with get_connection() as conn:
                await update_screenshot_ocr(conn, shot.id, ocr_text=None, ocr_status="skipped")
            continue
        if await is_skipped(shot.app_name):
            log.info(
                "ocr.skip_app",
                screenshot_id=shot.id,
                app_name=shot.app_name,
            )
            async with get_connection() as conn:
                await update_screenshot_ocr(conn, shot.id, ocr_text="", ocr_status="done")
            continue
        try:
            text = await asyncio.to_thread(
                _extract,
                Path(shot.thumbnail_path),
                configured_langs,
                settings.tesseract_path,
            )
        except OCRNotAvailable as exc:
            log.warning("ocr_worker.unavailable", error=str(exc))
            async with get_connection() as conn:
                await mark_pending_ocr_as_skipped(conn)
            return
        except Exception as exc:
            log.warning("ocr_worker.extract_failed", screenshot_id=shot.id, error=str(exc))
            async with get_connection() as conn:
                await update_screenshot_ocr(conn, shot.id, ocr_text=None, ocr_status="failed")
            continue

        cleaned, masks_applied = await apply_redaction(text)
        if masks_applied > 0:
            log.info(
                "ocr.redacted",
                screenshot_id=shot.id,
                masks_applied=masks_applied,
            )
        redacted = redact(cleaned)
        async with get_connection() as conn:
            await update_screenshot_ocr(conn, shot.id, ocr_text=redacted, ocr_status="done")
            try:
                await apply_rules_to_ocr(conn, screenshot_id=shot.id, ocr_text=redacted)
            except Exception as exc:
                log.warning(
                    "ocr_worker.regex_rules_failed",
                    screenshot_id=shot.id,
                    error=str(exc),
                )

        await _apply_phrase_tags(shot.id, redacted)
        await _store_word_confidences(
            screenshot_id=shot.id,
            thumbnail_path=Path(shot.thumbnail_path),
            langs=configured_langs,
            tesseract_path=settings.tesseract_path,
        )

        if settings.image_blur_enabled:
            try:
                regions_count, _ = await blur_sensitive_regions(Path(shot.thumbnail_path))
            except Exception as exc:
                log.warning(
                    "ocr_worker.image_blur_failed",
                    screenshot_id=shot.id,
                    error=str(exc),
                )
            else:
                if regions_count > 0:
                    async with get_connection() as conn:
                        await conn.execute(
                            "INSERT OR REPLACE INTO blur_applied "
                            "(screenshot_id, applied_at, regions_count) "
                            "VALUES (?, datetime('now'), ?)",
                            (shot.id, regions_count),
                        )
                        await conn.commit()
                    log.info(
                        "ocr.image_blurred",
                        screenshot_id=shot.id,
                        regions_count=regions_count,
                    )


async def _apply_phrase_tags(screenshot_id: int, ocr_text: str | None) -> None:
    """Run phrase-tag rules against the OCR text and tag the screenshot."""
    if not ocr_text:
        return
    try:
        phrase_tags = await apply_phrase_rules(ocr_text)
    except Exception as exc:
        log.warning(
            "ocr_worker.phrase_rules_failed",
            screenshot_id=screenshot_id,
            error=str(exc),
        )
        return

    applied: list[str] = []
    for tag_name in phrase_tags:
        try:
            async with get_connection() as conn:
                tag_id = await create_tag(conn, name=tag_name)
                await tag_screenshot(conn, screenshot_id, tag_id)
        except Exception as exc:
            log.warning(
                "ocr_worker.phrase_tag_apply_failed",
                screenshot_id=screenshot_id,
                tag=tag_name,
                error=str(exc),
            )
        else:
            applied.append(tag_name)

    if applied:
        log.info(
            "ocr.phrase_tag_applied",
            screenshot_id=screenshot_id,
            tags=applied,
        )


async def _resolve_langs(fallback: str) -> str:
    """Return the cached user-configured Tesseract language string.

    Falls back to ``fallback`` (typically ``settings.tesseract_langs``) when
    the configured-language lookup fails so the worker never stalls because
    of a malformed kv-settings row. The cache TTL lives inside
    :func:`refresh_ocr_lang_string`.
    """
    try:
        return await refresh_ocr_lang_string()
    except Exception as exc:
        log.warning("ocr_worker.langs_lookup_failed", error=str(exc))
        return fallback


def _extract(path: Path, langs: str, tesseract_path: Path | None) -> str:
    with Image.open(path) as image:
        image.load()
        return extract_text(image, langs=langs, tesseract_path=tesseract_path)


def _extract_word_data(
    path: Path,
    langs: str,
    tesseract_path: Path | None,
) -> list[tuple[str, int, int | None, int | None, int | None, int | None]]:
    """Run ``pytesseract.image_to_data`` and return ``(word, conf, l, t, w, h)``.

    Sync (CPU-bound) — call via ``asyncio.to_thread``. Rows with an empty
    ``text`` or ``conf < 0`` are filtered here so the caller can blindly
    insert what comes back. Returns ``[]`` if Tesseract is unavailable or
    raises — the worker pipeline must keep moving even when the per-word
    side-channel breaks.
    """
    try:
        import pytesseract  # noqa: PLC0415 — optional dep, lazy import
        from pytesseract import Output  # noqa: PLC0415
    except ImportError:
        return []

    if tesseract_path is not None:
        pytesseract.pytesseract.tesseract_cmd = str(tesseract_path)

    with Image.open(path) as image:
        image.load()
        raw_data = pytesseract.image_to_data(
            image,
            lang=langs,
            output_type=Output.DICT,
        )
    data: dict[str, list[object]] = dict(raw_data)

    words_raw = data.get("text", [])
    confs_raw = data.get("conf", [])
    lefts = data.get("left", [])
    tops = data.get("top", [])
    widths = data.get("width", [])
    heights = data.get("height", [])

    rows: list[tuple[str, int, int | None, int | None, int | None, int | None]] = []
    for idx, raw_word in enumerate(words_raw):
        word = str(raw_word).strip()
        if not word:
            continue
        conf = _safe_int(confs_raw[idx] if idx < len(confs_raw) else None)
        if conf is None or conf < 0:
            continue
        rows.append(
            (
                word,
                conf,
                _safe_int(lefts[idx] if idx < len(lefts) else None),
                _safe_int(tops[idx] if idx < len(tops) else None),
                _safe_int(widths[idx] if idx < len(widths) else None),
                _safe_int(heights[idx] if idx < len(heights) else None),
            )
        )
    return rows


def _safe_int(value: object) -> int | None:
    """Coerce a Tesseract bbox / conf cell to ``int``; return ``None`` on garbage."""
    if isinstance(value, bool | int | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


async def _store_word_confidences(
    *,
    screenshot_id: int,
    thumbnail_path: Path,
    langs: str,
    tesseract_path: Path | None,
) -> None:
    """Persist per-word ``conf`` rows for the v0.35 overlay.

    Wrapped end-to-end in ``try/except`` because this is a UI-only
    side-channel — the main OCR pipeline (text, redaction, tags) has
    already committed by the time we reach this point, so we must never
    let an ``image_to_data`` hiccup poison the worker loop.
    """
    try:
        rows = await asyncio.to_thread(
            _extract_word_data,
            thumbnail_path,
            langs,
            tesseract_path,
        )
    except Exception as exc:
        log.warning(
            "ocr_worker.words_extract_failed",
            screenshot_id=screenshot_id,
            error=str(exc),
        )
        return

    if not rows:
        log.info("ocr.words_stored", screenshot_id=screenshot_id, count=0)
        return

    try:
        async with get_connection() as conn:
            await conn.executemany(
                "INSERT INTO ocr_word "
                "(screenshot_id, word, conf, left, top, width, height) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(screenshot_id, *row) for row in rows],
            )
            await conn.commit()
    except Exception as exc:
        log.warning(
            "ocr_worker.words_insert_failed",
            screenshot_id=screenshot_id,
            error=str(exc),
        )
        return

    log.info("ocr.words_stored", screenshot_id=screenshot_id, count=len(rows))

    await _store_word_colours(
        screenshot_id=screenshot_id,
        thumbnail_path=thumbnail_path,
    )


async def _colour_columns_available() -> bool:
    """Return ``True`` iff ``ocr_word`` has the migration-049 colour columns.

    Probes ``PRAGMA table_info(ocr_word)`` exactly once and caches the
    answer in :data:`_colour_columns_present`. A missing migration
    silently disables colour sampling — the rest of the OCR pipeline is
    unaffected.
    """
    global _colour_columns_present  # noqa: PLW0603 — module-level probe cache
    if _colour_columns_present is not None:
        return _colour_columns_present
    try:
        async with get_connection() as conn:
            cursor = await conn.execute("PRAGMA table_info(ocr_word)")
            rows = await cursor.fetchall()
    except Exception as exc:
        colour_log.warning("ocr.colour.probe_failed", error=str(exc))
        _colour_columns_present = False
        return False
    cols = {str(row["name"]) for row in rows}
    _colour_columns_present = "bg_hex" in cols and "fg_hex" in cols
    if not _colour_columns_present:
        colour_log.info("ocr.colour.disabled", reason="migration_049_missing")
    return _colour_columns_present


def _sample_colours_batch(
    thumbnail_path: Path,
    rows: list[tuple[int, int, int, int, int]],
) -> list[tuple[int, str | None, str | None]]:
    """Open the image once and sample colours for each ``(word_id, l, t, w, h)``.

    Sync (PIL crop + quantize per word); invoke via ``anyio.to_thread``.
    Errors per-row collapse to ``(None, None)`` inside
    :func:`sample_colours`, so the batch always returns a result for
    every input row — even if every individual crop fails.
    """
    out: list[tuple[int, str | None, str | None]] = []
    try:
        with Image.open(thumbnail_path) as image:
            image.load()
            for word_id, left, top, width, height in rows:
                bg, fg = sample_colours(image, left, top, width, height)
                out.append((word_id, bg, fg))
    except Exception as exc:
        colour_log.warning(
            "ocr.colour.batch_failed",
            thumbnail_path=str(thumbnail_path),
            error=str(exc),
        )
    return out


async def _store_word_colours(
    *,
    screenshot_id: int,
    thumbnail_path: Path,
) -> None:
    """Sample bg/fg hex for up to :data:`COLOUR_SAMPLE_CAP` words of a shot.

    Reads the freshly-inserted ``ocr_word`` rows for ``screenshot_id``,
    sampled in insertion order (the same order Tesseract emits) so the
    cap deterministically picks the first N words on the screen.
    Updates each row with ``bg_hex`` / ``fg_hex``; rows where sampling
    failed get a ``NULL`` write (still valid per migration 049).
    """
    if not await _colour_columns_available():
        return

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT id, left, top, width, height FROM ocr_word "
                "WHERE screenshot_id = ? "
                "AND left IS NOT NULL AND top IS NOT NULL "
                "AND width IS NOT NULL AND height IS NOT NULL "
                "ORDER BY id "
                "LIMIT ?",
                (screenshot_id, COLOUR_SAMPLE_CAP),
            )
            db_rows = await cursor.fetchall()
    except Exception as exc:
        colour_log.warning(
            "ocr.colour.select_failed",
            screenshot_id=screenshot_id,
            error=str(exc),
        )
        return

    if not db_rows:
        return

    bbox_rows: list[tuple[int, int, int, int, int]] = [
        (
            int(row["id"]),
            int(row["left"]),
            int(row["top"]),
            int(row["width"]),
            int(row["height"]),
        )
        for row in db_rows
    ]

    try:
        sampled = await anyio.to_thread.run_sync(
            _sample_colours_batch,
            thumbnail_path,
            bbox_rows,
        )
    except Exception as exc:
        colour_log.warning(
            "ocr.colour.thread_failed",
            screenshot_id=screenshot_id,
            error=str(exc),
        )
        return

    if not sampled:
        return

    try:
        async with get_connection() as conn:
            await conn.executemany(
                "UPDATE ocr_word SET bg_hex = ?, fg_hex = ? WHERE id = ?",
                [(bg, fg, word_id) for word_id, bg, fg in sampled],
            )
            await conn.commit()
    except Exception as exc:
        colour_log.warning(
            "ocr.colour.update_failed",
            screenshot_id=screenshot_id,
            error=str(exc),
        )
        return

    hit = sum(1 for _, bg, fg in sampled if bg is not None and fg is not None)
    colour_log.info(
        "ocr.colour.stored",
        screenshot_id=screenshot_id,
        sampled=len(sampled),
        hit=hit,
    )
