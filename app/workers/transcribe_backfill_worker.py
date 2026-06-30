"""Ежедневная ретранскрипция «дырявых» аудио-сегментов — S12.

Whisper-бэкенд устанавливается опционально (``openai-whisper`` /
``faster-whisper``); до его установки ``audio_segment.transcript`` остаётся
NULL. Когда бэкенд появляется, нужен догоняющий проход по старым сегментам —
им и занимается этот воркер.

Обёрнут в :class:`app.workers._bases.BackfillRunner`, чтобы раскладка
lifespan-задач оставалась однородной с остальными backfill-воркерами
(audio-waveform, audio-merge, long-read, …).

* ``list_missing`` отдаёт до :data:`_BATCH_LIMIT` строк ``audio_segment``,
  где ``transcript IS NULL`` И есть аудио на диске (``path != ''`` — НЕ
  cold-сегмент, у которого байты уже выпилены retention-воркером, а
  транскрипт сохранён навсегда). Гейт: kv-килсвич, ``PERSONA_LEAN_MODE`` и
  доступность Whisper — если бэкенда нет, кандидаты не выбираются (тихий
  no-op, без спама в лог).
* ``build_one`` резолвит путь сегмента, гоняет :func:`transcribe_segment`
  (с locale-хинтом из строки, если он есть) и пишет результат в
  ``transcript``. ``None`` от бэкенда (файла нет / инференс упал) оставляет
  колонку NULL — следующий тик попробует снова.

Каденс — раз в сутки (86400 с): ретранскрипция дорогая (CPU-bound Whisper),
а свежие сегменты транскрибирует основной аудио-пайплайн в реальном времени;
этот воркер — лишь догоняющий backfill, ему достаточно суточного ритма.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from app.audio.transcribe import transcribe_segment
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.transcribe_backfill")


POLL_INTERVAL_SECONDS: Final[int] = 86400
"""Раз в сутки — см. докстринг модуля."""

_ENABLED_KV: Final[str] = "transcribe_backfill_enabled"
"""kv_settings-флаг (``0``/``1``) — рантайм-килсвич. Default-on (absence == on)."""

_WORKER_NAME: Final[str] = "transcribe-backfill-worker"

_BATCH_LIMIT: Final[int] = 25
"""Сколько NULL-сегментов берём за тик. Whisper медленный → батч маленький,
чтобы тик не висел минутами; остаток догонится на следующий день."""


def _lean_mode() -> bool:
    """LEAN_MODE — общий рубильник тяжёлых воркеров. Под ним ретранскрипция
    (CPU-bound) не должна крутиться; main.py и так гасит воркеры, но
    подстрахуемся самогейтом, чтобы прямой запуск тоже уважал флаг."""
    return os.environ.get("PERSONA_LEAN_MODE") == "1"


async def _is_enabled() -> bool:
    """``True``, пока ``transcribe_backfill_enabled`` не равен ровно ``0``.

    kv-строка создаётся лениво → отсутствие == включено (как у audio-waveform /
    audio-merge / alt-text воркеров). Best-effort: ошибка чтения kv → считаем
    выключенным, чтобы не гонять Whisper вслепую."""
    try:
        async with get_connection() as conn:
            value = await get_kv(conn, _ENABLED_KV)
    except Exception as exc:  # noqa: BLE001
        log.debug("transcribe_backfill.kv_failed", error=str(exc))
        return False
    if value is None:
        return True
    return value.strip() != "0"


def _whisper_available() -> bool:
    """Whisper-бэкенд установлен? Пробуем импорт без загрузки модели —
    дешёвая проверка, чтобы при отсутствии бэкенда вообще не дёргать БД.
    Best-effort: любой сбой импорта → считаем недоступным."""
    try:
        import importlib.util  # noqa: PLC0415

        return (
            importlib.util.find_spec("whisper") is not None
            or importlib.util.find_spec("faster_whisper") is not None
        )
    except Exception:  # noqa: BLE001
        return False


def _resolve_audio_path(stored: str, data_dir: Path) -> Path:
    """Абсолютный путь для значения ``audio_segment.path``.

    Воркер захвата пишет путь *относительно* ``data_dir`` (слэш-разделённый
    для кроссплатформенности); единичные edge-кейсы (перенос data_dir, ручная
    правка) могут оставить абсолютный путь. Обрабатываем обе формы — как в
    :mod:`app.workers.audio_retention_worker`."""
    candidate = Path(stored)
    if candidate.is_absolute():
        return candidate
    return data_dir / stored


async def _list_missing() -> list[int]:
    """``id`` сегментов без транскрипта, у которых ещё есть аудио на диске.

    Гейты (порядок — от дешёвого к дорогому): LEAN_MODE → kv-килсвич →
    наличие Whisper-бэкенда. Любой не пройден → пустой список (воркер просто
    спит). ``path != ''`` отсекает cold-сегменты (retention выпилил байты, но
    сохранил транскрипт — ретранскрибировать нечего). DESC — свежие первыми."""
    if _lean_mode():
        return []
    if not await _is_enabled():
        return []
    if not _whisper_available():
        return []
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id
                  FROM audio_segment
                 WHERE transcript IS NULL
                   AND path IS NOT NULL
                   AND path != ''
                   AND size_bytes > 0
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (int(_BATCH_LIMIT),),
            )
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.debug("transcribe_backfill.select_failed", error=str(exc))
        return []
    return [int(row["id"]) for row in rows]


async def _build_one(segment_id: int) -> str | None:
    """Ретранскрибировать один сегмент и записать результат в ``transcript``.

    Возвращает текст (для счётчика ``built`` в :class:`BackfillRunner`) или
    ``None``, если бэкенд вернул ``None`` (файла нет / инференс упал) —
    колонка остаётся NULL, следующий тик попробует снова. Внешние сбои
    (Whisper/ffmpeg) тихо гасятся ниже в ``transcribe_segment`` и здесь."""
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT path, locale FROM audio_segment WHERE id = ?",
                (int(segment_id),),
            )
            row = await cursor.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.debug("transcribe_backfill.row_failed", segment_id=int(segment_id), error=str(exc))
        return None
    if row is None:
        return None

    raw_path = row["path"]
    path_str = str(raw_path) if raw_path is not None else ""
    if not path_str.strip():
        return None

    settings = get_settings()
    resolved = _resolve_audio_path(path_str, settings.data_dir)
    if not resolved.exists():
        # Аудио нет на диске (cold/перенос) — транскрибировать нечего.
        log.debug("transcribe_backfill.file_missing", segment_id=int(segment_id), path=str(resolved))
        return None

    locale_hint = row["locale"] if "locale" in row.keys() else None
    locale_hint = str(locale_hint) if locale_hint else None

    text = await transcribe_segment(resolved, locale_hint=locale_hint)
    if text is None:
        # Бэкенд недоступен / инференс упал — колонка остаётся NULL.
        return None

    try:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE audio_segment SET transcript = ? WHERE id = ?",
                (text, int(segment_id)),
            )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("transcribe_backfill.update_failed", segment_id=int(segment_id), error=str(exc))
        return None

    log.info("transcribe_backfill.ok", segment_id=int(segment_id), chars=len(text))
    return text


async def run_transcribe_backfill_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan-вход: регистрирует :class:`BackfillRunner`.

    Имя задачи (``transcribe-backfill-worker``) попадает в heartbeat-таблицу,
    чтобы оператор видел зависший воркер в admin-health. Отмена приходит из
    shutdown-хендлера lifespan."""
    from app.workers._bases import BackfillRunner  # noqa: PLC0415

    runner = BackfillRunner(
        name=_WORKER_NAME,
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_list_missing,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = [
    "POLL_INTERVAL_SECONDS",
    "run_transcribe_backfill_worker",
]
