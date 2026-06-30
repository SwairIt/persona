"""Liveness probe and welcome / first-run wizard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import __version__
from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
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
    try:
        async with get_connection() as conn:
            await conn.execute("SELECT 1")
    except Exception:
        db_ok = False

    # Анонимный /health — минимум для балансировщиков. НЕ отдаём host/port/db_error/
    # captures_total: это фингерпринт инстанса + утечка внутреннего bind-адреса за
    # TLS-прокси + текст ошибки БД мог светить путь к файлу/драйвер (low-sev аудита).
    payload = {
        "version": __version__,
        "now": iso(datetime.now(timezone.utc)),
        "db_ok": db_ok,
        "paused": controller.paused,
        "ocr_enabled": settings.ocr_enabled,
        "tesseract_available": probe_tesseract(settings.tesseract_path).available,
    }
    status_code = 200 if db_ok else 503
    return JSONResponse(payload, status_code=status_code)


@router.get("/api/health/full")
async def health_full(
    _user: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Полный health-check инфраструктуры — ТОЛЬКО для владельца.

    Не кэшируется и закрыт owner-gate'ом, чтобы не светить наружу состояние
    БД/Ollama/диска. Каждая проба обёрнута в try/except + таймаут: любой
    внешний сбой (Ollama офлайн, нет tesseract, нет sqlite-vec) даёт
    осмысленный False/None, а не 500.
    """
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    if not await is_owner(int(_user["user_id"])):
        raise HTTPException(status_code=403, detail="Только владелец")

    settings = get_settings()

    # БД — лёгкий SELECT 1.
    db_ok = True
    try:
        async with get_connection() as conn:
            await conn.execute("SELECT 1")
    except Exception:  # noqa: BLE001
        db_ok = False

    # Ollama — GET <endpoint>/api/tags с коротким таймаутом.
    ollama_ready = False
    try:
        import httpx  # noqa: PLC0415

        from app.mcp.builtin_tools import _resolve_ollama_endpoint  # noqa: PLC0415

        endpoint = (await _resolve_ollama_endpoint()).rstrip("/")
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{endpoint}/api/tags")
            ollama_ready = resp.status_code == 200
    except Exception:  # noqa: BLE001
        ollama_ready = False

    # Tesseract — сначала which(), затем версия через pytesseract.
    tesseract_found = False
    try:
        tesseract_found = bool(probe_tesseract(settings.tesseract_path).available)
    except Exception:  # noqa: BLE001
        tesseract_found = False
    if not tesseract_found:
        try:
            tesseract_found = shutil.which("tesseract") is not None
        except Exception:  # noqa: BLE001
            tesseract_found = False

    # sqlite-vec — уже загруженный гейт ИЛИ свежая попытка импорта пакета.
    vec_available = False
    try:
        from app.storage.db import sqlite_vec_available  # noqa: PLC0415

        vec_available = bool(sqlite_vec_available())
    except Exception:  # noqa: BLE001
        vec_available = False
    if not vec_available:
        try:
            import sqlite_vec  # noqa: F401, PLC0415

            vec_available = True
        except Exception:  # noqa: BLE001
            vec_available = False

    # Свободное место на диске под data_dir (в мегабайтах).
    disk_free_mb: int | None = None
    try:
        usage = shutil.disk_usage(settings.data_dir)
        disk_free_mb = int(usage.free // (1024 * 1024))
    except Exception:  # noqa: BLE001
        disk_free_mb = None

    lean_mode = os.environ.get("PERSONA_LEAN_MODE") == "1"

    payload = {
        "db_ok": db_ok,
        "ollama_ready": ollama_ready,
        "tesseract_found": tesseract_found,
        "vec_available": vec_available,
        "disk_free_mb": disk_free_mb,
        "lean_mode": lean_mode,
        "version": __version__,
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/welcome", response_class=HTMLResponse)
async def welcome(
    request: Request,
    _user: Annotated[SessionRecord, Depends(current_user_required)],
    kind: str = Query(default=""),
) -> HTMLResponse:
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

    # T18 (2026-06-07) — sniff User-Agent so we can show the right
    # instructions per-device. iPhone gets the Shortcuts path; Mac gets
    # the one-line installer; Windows/Linux gets the generic info.
    ua = (request.headers.get("user-agent") or "").lower()
    if "iphone" in ua or "ipad" in ua or "ipod" in ua:
        device_kind = "iphone"
    elif "macintosh" in ua or "mac os x" in ua and "iphone" not in ua:
        device_kind = "mac"
    elif "windows" in ua:
        device_kind = "windows"
    elif "android" in ua:
        device_kind = "android"
    elif "linux" in ua:
        device_kind = "linux"
    else:
        device_kind = "other"

    # T29 — explicit ``?kind=`` overrides the UA sniff, so the user can read
    # iPhone instructions from their Mac (and vice-versa) via the switcher.
    kind_override = (kind or "").strip().lower()
    if kind_override in ("iphone", "mac", "windows", "android", "linux", "other"):
        device_kind = kind_override

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
            "device_kind": device_kind,
        },
    )
