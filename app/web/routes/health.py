"""Liveness probe and welcome / first-run wizard."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import __version__
from app.ocr import probe_tesseract
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.time import iso
from app.web.templates_engine import templates
from app.workers.control import get_controller

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> JSONResponse:
    controller = get_controller()
    settings = get_settings()
    db_ok = True
    db_error = None
    try:
        async with get_connection() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        db_ok = False
        db_error = str(exc)[:200]

    payload = {
        "version": __version__,
        "now": iso(datetime.now(timezone.utc)),
        "db_ok": db_ok,
        "db_error": db_error,
        "paused": controller.paused,
        "captures_total": controller.captures_total,
        "host": settings.host,
        "port": settings.port,
        "ocr_enabled": settings.ocr_enabled,
        "tesseract_available": probe_tesseract(settings.tesseract_path).available,
    }
    status_code = 200 if db_ok else 503
    return JSONResponse(payload, status_code=status_code)


@router.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request) -> HTMLResponse:
    """Onboarding pipeline page — shows which setup steps are done.

    T17 (2026-06-07): replaces the old static welcome.html with a
    live status view of the 3 setup tasks (Mac agent capturing, LLM
    configured, screenshot toggle on) plus a 'where to go next' grid.
    Each step lights up emerald once detected as done.
    """
    from app.auth import current_user_optional  # noqa: PLC0415
    from app.storage.db import get_connection  # noqa: PLC0415
    from app.storage.repository import get_kv  # noqa: PLC0415

    user_email = None
    try:
        user_session = await current_user_optional(request)
    except Exception:
        user_session = None
    if user_session is not None:
        user_email = user_session.get("email") if isinstance(user_session, dict) else None

    has_screenshots = False
    shots_count = 0
    last_shot_at: str | None = None
    has_llm = False
    llm_provider: str | None = None
    screens_disabled = True
    mic_paused = False

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n, MAX(captured_at) AS last FROM screenshots"
            )
            row = await cursor.fetchone()
            if row is not None:
                shots_count = int(row["n"] or 0)
                last_shot_at = (
                    str(row["last"]) if row["last"] is not None else None
                )
                has_screenshots = shots_count > 0

            llm_provider = await get_kv(conn, "llm_provider") or await get_kv(
                conn, "byo_api_provider"
            )
            if llm_provider:
                llm_provider_clean = llm_provider.strip().lower()
                if llm_provider_clean not in ("", "none"):
                    # Ollama has no API key — counts as configured if provider is set.
                    if llm_provider_clean == "ollama":
                        has_llm = True
                    else:
                        key_value = await get_kv(
                            conn, f"byo_api_key_{llm_provider_clean}"
                        ) or await get_kv(conn, "byo_api_key")
                        has_llm = bool(key_value and key_value.strip())

            screens_kill = await get_kv(conn, "capture_screens_disabled") or "0"
            screens_disabled = screens_kill.strip() == "1"
            mic_kill = await get_kv(conn, "audio_capture_paused_live") or "0"
            mic_paused = mic_kill.strip() == "1"
    except Exception:
        # Database might still be initializing — show defaults rather than 500.
        pass

    server_url = f"{request.url.scheme}://{request.url.netloc}"

    return templates.TemplateResponse(
        request,
        "welcome.html",
        {
            "title": "Привет",
            "active_nav": "",
            "user_email": user_email,
            "has_screenshots": has_screenshots,
            "shots_count": shots_count,
            "last_shot_at": last_shot_at,
            "has_llm": has_llm,
            "llm_provider": llm_provider or "—",
            "screens_disabled": screens_disabled,
            "mic_paused": mic_paused,
            "server_url": server_url,
        },
    )
