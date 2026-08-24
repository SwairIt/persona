"""UI theme settings — GET form, POST persists to ``kv_settings`` (v0.32).

A single ``theme`` row in ``kv_settings`` drives the ``<html>`` class on
:file:`base.html`. The whitelist is intentionally minimal — ``dark``,
``light``, ``auto`` — so the POST handler can reject anything else with
a 400 instead of writing junk that would silently fall back to the
default on the next render.

``auto`` defers to the user's OS preference via ``window.matchMedia``;
the inline script lives in :file:`base.html` because it must run before
first paint to avoid a flash of the wrong palette.

Чья тема (2026-08)
------------------
У роутера НЕ БЫЛО зависимости аутентификации, а строка ``theme`` одна на
инстанс: любой зарегистрированный участник перекрашивал сайт ВСЕМ, включая
владельца. Теперь нужна сессия, и адрес записи выбирает личность —
владелец пишет глобальный ``kv_settings``, участник свою строку в
``user_settings`` (её читает Jinja-глобал ``get_theme`` через
:data:`app.request_ctx.current_member_uid`). Тут же живёт per-user
переключатель языка интерфейса: ``POST /api/settings/ui-language``.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    UI_LANGUAGE_KV_KEY,
    invalidate_language_cache,
)
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, get_user_kv, set_kv, set_user_kv
from app.web.templates_engine import (
    invalidate_theme_cache,
    invalidate_user_kv_sync,
    templates,
)

router = APIRouter(tags=["settings"])
log = get_logger("persona.theme")

_VALID_THEMES: Final[frozenset[str]] = frozenset({"dark", "light", "auto", "persona", "cosmos", "cosmos-dark"})
_DEFAULT_THEME: Final[str] = "persona"


async def _member_uid(session: SessionRecord) -> int | None:
    """``None`` для владельца (глобальный kv), иначе id участника.

    Сбой резолва владельца → трактуем как участника: хуже перекрасить весь
    инстанс чужим выбором, чем записать настройку не туда.
    """
    uid = session["user_id"]
    try:
        owner = await is_owner(uid)
    except Exception:  # noqa: BLE001 — сбой гейта → не владелец
        owner = False
    return None if owner else int(uid)


async def _load_theme(uid: int | None) -> str:
    """Read the stored theme, defaulting to :data:`_DEFAULT_THEME` if absent."""
    async with get_connection() as conn:
        raw = (
            await get_kv(conn, "theme")
            if uid is None
            else await get_user_kv(conn, uid, "theme")
        )
    if raw is None or raw not in _VALID_THEMES:
        return _DEFAULT_THEME
    return raw


async def _load_language(uid: int | None) -> str:
    async with get_connection() as conn:
        raw = (
            await get_kv(conn, UI_LANGUAGE_KV_KEY)
            if uid is None
            else await get_user_kv(conn, uid, UI_LANGUAGE_KV_KEY)
        )
    value = (raw or "").strip()
    return value if value in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


@router.get("/settings/theme", response_class=HTMLResponse)
async def theme_settings_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Render the radio form with the viewer's OWN stored theme pre-selected."""
    uid = await _member_uid(session)
    current = await _load_theme(uid)
    return templates.TemplateResponse(
        request,
        "theme_settings.html",
        {
            "title": "Theme",
            "active_nav": "settings",
            "current": current,
            "language_current": await _load_language(uid),
            "language_options": sorted(SUPPORTED_LANGUAGES),
            "options": (
                ("cosmos", "Cosmos 🌌", "Космос: живой 3D-фон со звёздами, туманностями и планетой-ядром."),
                ("cosmos-dark", "Cosmos Dark 🌑", "Тот же космос, но глубже и темнее — приглушённый фон, максимальный контраст."),
                ("persona", "Persona ✨", "Фиолетовая тема в стиле лендинга — градиенты и стекло."),
                ("dark", "Dark", "Тёмная минималистичная палитра."),
                ("light", "Light", "Светлая палитра для яркой комнаты."),
                ("auto", "Auto", "Следовать системной теме ОС."),
            ),
        },
    )


@router.post("/settings/theme")
async def theme_settings_save(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    theme: str = Form(...),
) -> RedirectResponse:
    """Validate against the whitelist and persist; reject anything else."""
    value = theme.strip().lower()
    if value not in _VALID_THEMES:
        log.warning("theme.settings.rejected", value=value)
        raise HTTPException(status_code=400, detail="invalid theme")

    uid = await _member_uid(session)
    async with get_connection() as conn:
        if uid is None:
            await set_kv(conn, "theme", value)
        else:
            await set_user_kv(conn, uid, "theme", value)

    # The Jinja global caches per-request; this POST already cached the
    # *previous* value during form-render-time helpers, so drop it so
    # the redirect-target render reflects the new value immediately.
    if uid is None:
        invalidate_theme_cache()
    else:
        # Участник: сбрасываем и per-request кэш темы, и процесс-TTL-кэш ЕГО
        # строки — иначе следующий рендер до 15 с показывал бы старую тему.
        invalidate_theme_cache()
        invalidate_user_kv_sync(uid, "theme")

    log.info("theme.settings.saved", theme=value, owner=uid is None)
    return RedirectResponse(url="/settings/theme", status_code=303)


@router.post("/api/settings/ui-language")
async def ui_language_save(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    language: str = Form(...),
) -> RedirectResponse:
    """Язык интерфейса: владелец → глобальный kv, участник → ``user_settings``.

    Глобальная строка ``ui_language`` переключала интерфейс ВСЕМ (её пишет
    owner-only ``POST /settings/ui-language``). Этот эндпоинт — member-safe
    близнец: участник меняет язык только себе, владелец — как раньше, всему
    инстансу. Неизвестный код схлопывается в :data:`DEFAULT_LANGUAGE`, а не
    пишется дословно, чтобы рендер не остался без таблицы переводов.
    """
    candidate = language.strip().lower()
    value = candidate if candidate in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    uid = await _member_uid(session)
    async with get_connection() as conn:
        if uid is None:
            await set_kv(conn, UI_LANGUAGE_KV_KEY, value)
        else:
            await set_user_kv(conn, uid, UI_LANGUAGE_KV_KEY, value)
    invalidate_language_cache()
    if uid is not None:
        invalidate_user_kv_sync(uid, UI_LANGUAGE_KV_KEY)
    log.info("i18n.language.set", language=value, requested=candidate, owner=uid is None)
    return RedirectResponse(url="/settings/theme", status_code=303)
