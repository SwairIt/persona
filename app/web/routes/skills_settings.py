"""Навыки (skills) — /settings/skills.

UI для устанавливаемых навыков (app/skills/store.py): список, установка из
GitHub (SKILL.md/README.md — только текст, код не выполняется), вкл/выкл,
удаление. Включённые навыки подмешиваются в системный промпт чата
(enabled_skills_prompt). Server-rendered + form-POST, работает без JS.

Также `GET /api/skills` (JSON) для слэш-команды `/skill` и палитры.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.skills.store import (
    delete_skill,
    fetch_skill_from_github,
    list_skills,
    save_skill,
    set_skill_enabled,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.skills.settings")


async def _render(request: Request, user_id: int, *, error: str = "", saved: str = "") -> HTMLResponse:
    from app.skills.builtin import seed_builtin_skills  # noqa: PLC0415

    await seed_builtin_skills(user_id)  # встроенные навыки в каталоге (один раз)
    items = await list_skills(user_id)
    return templates.TemplateResponse(
        request,
        "skills_settings.html",
        {
            "title": "Навыки — что умеет ассистент",
            "active_nav": "settings",
            "items": items,
            "count": len(items),
            "error": error,
            "saved": saved,
        },
    )


@router.get("/settings/skills", response_class=HTMLResponse)
async def skills_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    return await _render(request, session["user_id"])


@router.post("/settings/skills/install", response_model=None)
async def skills_install(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    url: str = Form(default=""),
) -> HTMLResponse | RedirectResponse:
    url = (url or "").strip()
    if not url:
        return RedirectResponse("/settings/skills", status_code=303)
    try:
        name, content, raw_url = await fetch_skill_from_github(url)
        await save_skill(session["user_id"], name, content, raw_url)
    except ValueError as exc:
        return await _render(request, session["user_id"], error=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.warning("skill.install_failed", url=url, error=str(exc))
        return await _render(request, session["user_id"], error="не удалось установить навык")
    return RedirectResponse("/settings/skills", status_code=303)


@router.post("/settings/skills/{skill_id}/toggle", response_model=None)
async def skills_toggle(
    skill_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    enabled: str = Form(default=""),
) -> RedirectResponse:
    await set_skill_enabled(session["user_id"], skill_id, bool(enabled))
    return RedirectResponse("/settings/skills", status_code=303)


@router.post("/settings/skills/{skill_id}/delete", response_model=None)
async def skills_delete(
    skill_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    await delete_skill(session["user_id"], skill_id)
    return RedirectResponse("/settings/skills", status_code=303)


@router.get("/api/skills", response_class=JSONResponse)
async def api_skills_list(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Список навыков пользователя (для /skill и палитры)."""
    return JSONResponse({"skills": await list_skills(session["user_id"])})


__all__ = ["router"]
