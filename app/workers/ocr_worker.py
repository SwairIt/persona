"""Background OCR worker — drains screenshots with pending OCR status."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from pathlib import Path

import anyio
from PIL import Image

from app.image_blur import blur_sensitive_regions
from app.logging_setup import get_logger
from app.ocr import OCRNotAvailable, extract_text, is_available, redact
from app.ocr.colour_sample import sample_colours
from app.ocr.language_stats import _BUCKETS, _classify
from app.ocr.languages import refresh_ocr_lang_string
from app.ocr_phrase_tags import apply_phrase_rules
from app.ocr_sentiment import score as score_sentiment
from app.redaction import apply_redaction
from app.settings import get_settings
from app.shot_colours import compute_palette
from app.storage.db import get_connection
from app.storage.ocr_skip import is_skipped
from app.storage.regex_rules import apply_rules_to_ocr
from app.storage.repository import (
    list_pending_ocr,
    mark_pending_ocr_as_skipped,
    update_screenshot_ocr,
)
from app.storage.tags import create_tag, tag_screenshot
from app.tag_aliases import resolve as resolve_tag_alias
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.ocr_worker")
colour_log = get_logger("persona.ocr.colour")
lang_log = get_logger("persona.lang_autodetect_insert")
sentiment_log = get_logger("persona.ocr.sentiment")
shot_colours_log = get_logger("persona.shot_colours")

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


# ── Параллельный OCR через процессы ───────────────────────────────────────
# Tesseract — CPU-bound, поэтому asyncio.to_thread не даёт настоящего
# параллелизма (упирается в GIL). ProcessPoolExecutor обходит GIL: каждый
# распознаваемый кадр уходит в отдельный процесс. Аргументы строго
# picklable (путь к файлу + строки конфига, НЕ объекты PIL) — это важно для
# Windows, где пул использует spawn и пиклит и функцию, и её аргументы.

_PROCESS_POOL: ProcessPoolExecutor | None = None
"""Ленивый, переиспользуемый пул процессов (один на воркер).

``None`` до первого использования и после ``shutdown`` сломанного пула.
:func:`_get_process_pool` создаёт его по требованию; при любой ошибке
создания возвращает ``None`` → вызывающий код тихо откатывается на
``asyncio.to_thread`` (старый путь).
"""

_PROCESS_POOL_DISABLED = False
"""Флаг «пул недоступен навсегда в этом процессе».

Ставится в ``True``, если создать пул не удалось или он сломался
(``BrokenExecutor``). После этого все вызовы идут через thread-fallback,
не пытаясь пересоздать заведомо нерабочий пул.
"""


def _resolve_ocr_workers() -> int:
    """Сколько процессов держать в OCR-пуле.

    ``PERSONA_OCR_WORKERS`` из env переопределяет дефолт
    ``min(3, (os.cpu_count() or 2) - 1)``. Нижняя граница — 1 (иначе
    ``ProcessPoolExecutor`` бросит ``ValueError`` на ``max_workers=0``).
    Мусор в env → дефолт.
    """
    default = min(3, (os.cpu_count() or 2) - 1)
    default = max(1, default)
    raw = os.environ.get("PERSONA_OCR_WORKERS")
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        log.warning("ocr_worker.bad_workers_env", value=raw, fallback=default)
        return default
    return max(1, value)


def _get_process_pool() -> ProcessPoolExecutor | None:
    """Вернуть переиспользуемый пул процессов или ``None``.

    Создаёт пул лениво и кэширует его в :data:`_PROCESS_POOL`. Если пул
    ранее сломался / отключён (:data:`_PROCESS_POOL_DISABLED`) или его не
    удаётся создать — возвращает ``None``, и вызывающий код уходит на
    thread-fallback. Best-effort: ни одна ошибка отсюда не должна ронять
    воркер.
    """
    global _PROCESS_POOL, _PROCESS_POOL_DISABLED  # noqa: PLW0603 — кэш пула
    if _PROCESS_POOL_DISABLED:
        return None
    if _PROCESS_POOL is not None:
        return _PROCESS_POOL
    try:
        workers = _resolve_ocr_workers()
        _PROCESS_POOL = ProcessPoolExecutor(max_workers=workers)
    except Exception as exc:
        log.warning("ocr_worker.pool_create_failed", error=str(exc))
        _PROCESS_POOL = None
        _PROCESS_POOL_DISABLED = True
        return None
    log.info("ocr_worker.pool_started", workers=workers)
    return _PROCESS_POOL


def _disable_process_pool() -> None:
    """Пометить пул сломанным и закрыть его — дальше только thread-fallback."""
    global _PROCESS_POOL, _PROCESS_POOL_DISABLED  # noqa: PLW0603 — кэш пула
    _PROCESS_POOL_DISABLED = True
    pool = _PROCESS_POOL
    _PROCESS_POOL = None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001 — закрытие сломанного пула best-effort
            pass


def _shutdown_process_pool() -> None:
    """Закрыть пул при остановке воркера (не помечая его «навсегда сломанным»).

    В отличие от :func:`_disable_process_pool`, НЕ ставит
    :data:`_PROCESS_POOL_DISABLED` — если воркер перезапустят в том же
    процессе, пул создастся заново. Best-effort: ошибки закрытия глушим.
    """
    global _PROCESS_POOL  # noqa: PLW0603 — кэш пула
    pool = _PROCESS_POOL
    _PROCESS_POOL = None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001 — закрытие пула best-effort
            pass


async def _run_in_pool(func, *args):  # noqa: ANN001, ANN002, ANN202 — generic dispatch
    """Выполнить CPU-bound ``func(*args)`` в пуле процессов.

    Аргументы обязаны быть picklable. Сначала пытается прогнать через
    :data:`_PROCESS_POOL`; при недоступности пула или ``BrokenExecutor``
    (сломался mid-flight — типично для Windows spawn-сбоев) тихо
    откатывается на ``asyncio.to_thread`` (старый путь), чтобы
    пост-обработка не пострадала. ``OCRNotAvailable`` пробрасывается
    наверх — это доменная ошибка, её обрабатывает вызывающий код.
    """
    pool = _get_process_pool()
    if pool is not None:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(pool, func, *args)
        except OCRNotAvailable:
            raise
        except (BrokenExecutor, OSError, RuntimeError) as exc:
            # Сломанный/закрытый пул (Windows spawn-pickling, OOM, гонка на
            # shutdown). Глушим пул и доигрываем этот кадр в потоке.
            log.warning("ocr_worker.pool_broken", error=str(exc))
            _disable_process_pool()
    return await asyncio.to_thread(func, *args)


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

    try:
        while not ctrl.stop_event.is_set():
            await beat("ocr-worker")
            try:
                await _drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("ocr_worker.iteration_failed", error=str(exc))

            try:
                await asyncio.wait_for(
                    ctrl.stop_event.wait(), timeout=POLL_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                continue
    finally:
        # Закрываем пул процессов при выходе (stop / cancel), чтобы не
        # осиротить дочерние процессы Tesseract на Windows.
        _shutdown_process_pool()


async def _drain_once() -> None:  # noqa: PLR0915 — pipeline orchestration, each step is a single dispatch
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
            text = await _run_in_pool(
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

        await _store_dominant_script(screenshot_id=shot.id, ocr_text=redacted)

        await _store_sentiment(screenshot_id=shot.id, ocr_text=redacted)

        await _store_shot_palette(screenshot_id=shot.id)

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
                    # v1.30 — больше не пишем в blur_applied: grep по
                    # codebase + tests показал zero readers с момента
                    # появления таблицы (migration 021). Сохраняем только
                    # лог-запись на случай дебага.
                    log.info(
                        "ocr.image_blurred",
                        screenshot_id=shot.id,
                        regions_count=regions_count,
                    )


def _dominant_script(ocr_text: str) -> str | None:
    """Return the bucket name with the highest character count, or ``None``.

    Walks ``ocr_text`` character-by-character through the v0.39
    :func:`app.ocr.language_stats._classify` helper, then picks the
    bucket with the highest total. Ties are broken by the canonical
    bucket order from :data:`app.ocr.language_stats._BUCKETS` —
    deterministic and stable across re-runs of the same input.

    Returns ``None`` when the text is empty or contains no classifiable
    glyphs (every count is zero). The caller skips the UPDATE in that
    case so the column stays ``NULL`` rather than landing in
    ``'other'`` for a whitespace-only shot.
    """
    if not ocr_text:
        return None
    counts: dict[str, int] = dict.fromkeys(_BUCKETS, 0)
    for ch in ocr_text:
        counts[_classify(ch)] += 1
    best: str | None = None
    best_count = 0
    # Iterate ``_BUCKETS`` (not ``counts.items()``) so a tie deterministically
    # falls to the earliest-declared bucket — ``cyrillic`` beats ``latin``
    # beats ``cjk`` beats ``digit`` beats ``other``. Without a fixed order,
    # tie-breaking would depend on dict insertion order, which is stable
    # in CPython but conceptually accidental.
    for bucket in _BUCKETS:
        if counts[bucket] > best_count:
            best = bucket
            best_count = counts[bucket]
    return best


async def _store_dominant_script(*, screenshot_id: int, ocr_text: str | None) -> None:
    """Classify the OCR text and persist the dominant-script bucket label.

    Best-effort side-channel: the OCR text + status have already been
    committed by the time this runs, so any failure here must not poison
    the worker loop. Errors are logged at ``warning`` and swallowed.
    Empty / unclassifiable text writes nothing (column stays ``NULL``);
    that mirrors how the search filter treats unknowns — they drop out
    of a ``?script=...`` query, they're not coerced into ``'other'``.
    """
    if not ocr_text:
        return
    script = _dominant_script(ocr_text)
    if script is None:
        return
    try:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE screenshots SET dominant_script = ? WHERE id = ?",
                (script, screenshot_id),
            )
            await conn.commit()
    except Exception as exc:
        lang_log.warning(
            "lang_autodetect_insert.update_failed",
            screenshot_id=screenshot_id,
            error=str(exc),
        )
        return
    lang_log.info(
        "lang_autodetect_insert.stored",
        screenshot_id=screenshot_id,
        dominant_script=script,
        chars=len(ocr_text),
    )


async def _store_sentiment(*, screenshot_id: int, ocr_text: str | None) -> None:
    """Score the OCR text against the bundled lexicon and persist the polarity.

    Best-effort side-channel: the OCR text + status have already been
    committed by the time this runs, so any failure here must not
    poison the worker loop. Empty / unscorable text leaves the column
    ``NULL`` (the route's "no signal" semantic — see migration 087)
    rather than coercing it to ``0.0``, so the dashboard can
    distinguish a *truly* neutral shot from one that simply had no
    text to score.

    :func:`app.ocr_sentiment.score` clamps its return value to
    ``[-1.0, +1.0]`` already; this wrapper only handles persistence
    and the empty-text early-out.
    """
    if not ocr_text:
        return
    try:
        polarity = score_sentiment(ocr_text)
    except Exception as exc:
        sentiment_log.warning(
            "ocr.sentiment.score_failed",
            screenshot_id=screenshot_id,
            error=str(exc),
        )
        return

    try:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE screenshots SET sentiment = ? WHERE id = ?",
                (polarity, screenshot_id),
            )
            await conn.commit()
    except Exception as exc:
        sentiment_log.warning(
            "ocr.sentiment.update_failed",
            screenshot_id=screenshot_id,
            error=str(exc),
        )
        return

    sentiment_log.info(
        "ocr.sentiment.stored",
        screenshot_id=screenshot_id,
        polarity=polarity,
        chars=len(ocr_text),
    )


async def _store_shot_palette(*, screenshot_id: int) -> None:
    """Compute the dominant-colour palette and cache it via :mod:`app.shot_colours`.

    Best-effort side-channel mirroring :func:`_store_sentiment` and
    :func:`_store_dominant_script` — the OCR text + status have already
    been committed by the time this runs, so any palette failure must
    never poison the worker loop. :func:`compute_palette` already
    swallows every internal error and returns ``None`` on failure; the
    enclosing ``try/except`` here is defence-in-depth for anything
    that might leak past it (a future refactor, a thread-pool oom,
    etc.). The cache write happens inside ``compute_palette`` itself,
    so this helper has no DB work of its own.
    """
    try:
        result = await compute_palette(screenshot_id)
    except Exception as exc:
        shot_colours_log.warning(
            "shot_colours.worker_failed",
            screenshot_id=screenshot_id,
            error=str(exc),
        )
        return

    if result is None:
        shot_colours_log.debug(
            "shot_colours.worker_skipped",
            screenshot_id=screenshot_id,
        )
        return

    shot_colours_log.info(
        "shot_colours.worker_stored",
        screenshot_id=screenshot_id,
        entries=len(result),
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

    # Route every phrase-rule tag through the alias overlay before it
    # touches the tag store, so an operator can collapse equivalent
    # spellings ("standup" / "daily-standup") onto a single canonical
    # facet without rewriting every phrase rule.
    applied: list[str] = []
    seen: set[str] = set()
    for raw in phrase_tags:
        canonical = await resolve_tag_alias(raw)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        try:
            async with get_connection() as conn:
                tag_id = await create_tag(conn, name=canonical)
                await tag_screenshot(conn, screenshot_id, tag_id)
        except Exception as exc:
            log.warning(
                "ocr_worker.phrase_tag_apply_failed",
                screenshot_id=screenshot_id,
                tag=canonical,
                error=str(exc),
            )
        else:
            applied.append(canonical)

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
        rows = await _run_in_pool(
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
