"""Background classifier for OCR text → ``ocr_looks_like_code``.

Re-uses the shared :mod:`app.workers._bases` ``BackfillRunner``
machinery — every 30 minutes the worker scans the most recent
unflagged OCR texts and updates the bit. The kv flag
``ocr_code_detection_enabled`` (default ``1``) gates the sweep so users
can turn it off without restarting the process.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.ocr_code_detector import classify_recent
from app.settings.effective import _coerce_bool, get_effective
from app.workers._bases import BackfillRunner

log = get_logger("persona.ocr_code_detector_worker")

_POLL_SECONDS = 1800


async def _enabled() -> bool:
    raw = await get_effective("ocr_code_detection_enabled")
    if raw is None:
        return True
    return _coerce_bool(raw)


async def _list_missing() -> list[int]:
    if not await _enabled():
        return []
    return [1]


async def _build_one(_sentinel: int) -> dict[str, Any] | None:
    result = await classify_recent(limit=200)
    return result if result.get("classified", 0) > 0 else None


async def run_ocr_code_detector_worker() -> None:
    """Lifespan entry-point for the OCR code-detection backfill."""
    runner = BackfillRunner(
        name="ocr-code-detector",
        poll_seconds=_POLL_SECONDS,
        list_missing=_list_missing,
        build_one=_build_one,
    )
    await runner.run()
