"""HTTP surface for the voice-segment auto-translate feature (v1.18).

Three JSON endpoints:

* ``GET  /api/audio/translate`` — return the gate state + target
  language. Used by the settings UI to render the toggle.
* ``POST /api/audio/translate`` — flip the kv gate. Body is JSON
  ``{"enabled": bool}``. Returns ``{"ok": true, "enabled": <bool>}``.
* ``GET  /api/audio-segment/{id}/translation.json`` — return the per-
  row transcript + translation + detected source language. Used by
  the segment detail page to render the translated text alongside
  the original.

The route deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — wire it up in the route-coordinator instead::

    from app.web.routes import auto_translate_settings
    app.include_router(auto_translate_settings.router)
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

router = APIRouter(tags=["audio-auto-translate"])
log = get_logger("persona.audio.auto_translate.web")

_KV_ENABLED: Final[str] = "audio_auto_translate_enabled"
"""Gate kv row — must match
:data:`app.workers.auto_translate_worker._KV_ENABLED`."""


async def _read_enabled() -> bool:
    """Return ``True`` iff the gate kv row holds the string ``"1"``."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return False
    return raw.strip() == "1"


async def _resolve_target_lang() -> str:
    """Best-effort UI language lookup for the GET response.

    Mirrors the worker's resolver — we don't import the worker module
    to avoid a circular dependency between the routes package and the
    workers package.
    """
    try:
        from app.i18n import get_ui_language  # noqa: PLC0415
    except Exception as exc:
        log.warning("auto_translate.web.i18n_import_failed", error=str(exc))
        return "en"

    try:
        value = get_ui_language()
    except Exception as exc:
        log.warning("auto_translate.web.i18n_read_failed", error=str(exc))
        return "en"
    return value or "en"


@router.get("/api/audio/translate", response_class=JSONResponse)
async def get_auto_translate_settings() -> JSONResponse:
    """Return the current gate state + resolved target language."""
    enabled = await _read_enabled()
    target_lang = await _resolve_target_lang()
    log.info(
        "auto_translate.web.get",
        enabled=enabled,
        target_lang=target_lang,
    )
    return JSONResponse({"enabled": enabled, "target_lang": target_lang})


@router.post("/api/audio/translate", response_class=JSONResponse)
async def set_auto_translate_settings(request: Request) -> JSONResponse:
    """Flip the gate kv row. Body is JSON ``{"enabled": bool}``.

    Anything that isn't a JSON object with a boolean ``enabled`` key
    is rejected with HTTP 400 — we treat the API as strict input even
    though a misclick from the UI is the most common caller.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        log.warning("auto_translate.web.bad_json", error=str(exc))
        raise HTTPException(status_code=400, detail="invalid_json") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected_json_object")

    raw_enabled = payload.get("enabled")
    if not isinstance(raw_enabled, bool):
        raise HTTPException(status_code=400, detail="enabled_must_be_bool")

    async with get_connection() as conn:
        await set_kv(conn, _KV_ENABLED, "1" if raw_enabled else "0")

    log.info("auto_translate.web.set", enabled=raw_enabled)
    return JSONResponse({"ok": True, "enabled": raw_enabled})


@router.get(
    "/api/audio-segment/{seg_id}/translation.json",
    response_class=JSONResponse,
)
async def get_segment_translation(seg_id: int) -> JSONResponse:
    """Return the per-row transcript / translation / source-language.

    Returns HTTP 404 when the segment row does not exist. The other
    three fields may independently be ``null`` — the caller renders
    "no transcript" / "not yet translated" / "language unknown" labels.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT transcript, transcript_translated, source_language "
            "FROM audio_segment WHERE id = ?",
            (seg_id,),
        )
        row = await cursor.fetchone()

    if row is None:
        log.info("auto_translate.web.segment.missing", seg_id=seg_id)
        raise HTTPException(status_code=404, detail="segment_not_found")

    transcript = None if row["transcript"] is None else str(row["transcript"])
    translated = (
        None if row["transcript_translated"] is None else str(row["transcript_translated"])
    )
    source_language = (
        None if row["source_language"] is None else str(row["source_language"])
    )

    log.info(
        "auto_translate.web.segment.read",
        seg_id=seg_id,
        has_transcript=transcript is not None,
        has_translation=translated is not None,
        source_language=source_language,
    )

    return JSONResponse(
        {
            "seg_id": seg_id,
            "source_language": source_language,
            "transcript": transcript,
            "transcript_translated": translated,
        }
    )


__all__ = ["router"]
