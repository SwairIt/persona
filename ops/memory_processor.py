"""T29 MVP шаг3c — standalone memory processor.

Runs as a SEPARATE process (scheduled every few minutes), NOT inside the
web server, so heavy work (OCR/embeddings) can NEVER block or crash the
web event loop — the whole reason lean-mode disabled these workers.

Each step is isolated (own try/except) and bounded (small batches), and a
lock file prevents overlapping runs. What it does per run:
  1. Build/refresh hourly cards for the last few hours — NO extra deps,
     deterministic aggregation of your activity. This is the memory the
     chat injects (app/memory_context.py), so it stays current.
  2. OCR a bounded batch — ONLY if tesseract is installed + enabled.
  3. Embeddings a bounded batch — ONLY if fastembed is installed (needed
     for future semantic search over screen text).

If a dep is missing the step is skipped cleanly. Run:
    python -m ops.memory_processor
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Make `import app` work when run by file path (scheduled task runs
# `pythonw ops/memory_processor.py`, so the repo root isn't on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_LOCK = Path.home() / ".persona" / "memproc.lock"
_LOCK_STALE_SECONDS = 1800  # 30 min — a run should never take this long


def _acquire_lock() -> bool:
    """Best-effort single-instance lock. Returns False if another run is live."""
    try:
        if _LOCK.exists():
            age = time.time() - _LOCK.stat().st_mtime
            if age < _LOCK_STALE_SECONDS:
                return False
        _LOCK.parent.mkdir(parents=True, exist_ok=True)
        _LOCK.write_text(str(time.time()), encoding="utf-8")
        return True
    except OSError:
        return True  # if the lock fs is flaky, don't block processing


def _release_lock() -> None:
    try:
        _LOCK.unlink(missing_ok=True)
    except OSError:
        pass


async def _amain() -> int:
    from app.logging_setup import get_logger  # noqa: PLC0415
    from app.settings import get_settings  # noqa: PLC0415
    from app.storage.db import init_database  # noqa: PLC0415

    log = get_logger("persona.memproc")
    await init_database()
    s = get_settings()

    # 1. Hourly cards — zero deps, always run. Refreshes the last 6 hours
    # (idempotent upsert) so the chat's memory of recent activity is current.
    try:
        from app.hourly_card import build_card_for_hour  # noqa: PLC0415

        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        built = 0
        for h in range(1, 7):
            res = await build_card_for_hour(now - timedelta(hours=h))
            if res:
                built += 1
        log.info("memproc.cards", built=built)
        print(f"[memproc] hourly cards refreshed: {built}", flush=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("memproc.cards_failed", error=str(exc))
        print(f"[memproc] cards error: {exc}", flush=True)

    # 2. OCR — only if tesseract present + enabled. Bounded.
    try:
        from app.ocr import is_available as ocr_available  # noqa: PLC0415

        if getattr(s, "ocr_enabled", False) and ocr_available(
            getattr(s, "tesseract_path", None)
        ):
            from app.workers.ocr_worker import _drain_once as ocr_drain  # noqa: PLC0415

            for _ in range(20):
                await ocr_drain()
            log.info("memproc.ocr_done")
            print("[memproc] OCR batch done", flush=True)
        else:
            print("[memproc] OCR skipped (tesseract/enable off)", flush=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("memproc.ocr_failed", error=str(exc))
        print(f"[memproc] OCR error: {exc}", flush=True)

    # 3. Embeddings — only if fastembed present. Bounded.
    try:
        from app.embeddings import is_available as emb_available  # noqa: PLC0415

        if emb_available():
            from app.workers.embeddings_worker import _drain_once as emb_drain  # noqa: PLC0415

            for _ in range(20):
                await emb_drain()
            log.info("memproc.embeddings_done")
            print("[memproc] embeddings batch done", flush=True)
        else:
            print("[memproc] embeddings skipped (fastembed not installed)", flush=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("memproc.embeddings_failed", error=str(exc))
        print(f"[memproc] embeddings error: {exc}", flush=True)

    return 0


def main() -> int:
    if not _acquire_lock():
        print("[memproc] another run is in progress — skipping", flush=True)
        return 0
    try:
        return asyncio.run(_amain())
    finally:
        _release_lock()


if __name__ == "__main__":
    sys.exit(main())
