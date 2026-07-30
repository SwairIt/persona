"""Owner-only read model for Telegram people and their retained context."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.integrations.telegram.people import TelegramPeopleRepository
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
_people = TelegramPeopleRepository()


async def _owner_id(session: SessionRecord) -> int:
    user_id = int(session["user_id"])
    if not await is_owner(user_id):
        raise HTTPException(status_code=403, detail="Только владелец")
    return user_id


@router.get("/settings/telegram-people", response_class=HTMLResponse)
async def telegram_people_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    user_id = await _owner_id(session)
    return templates.TemplateResponse(
        request,
        "telegram_people.html",
        {
            "title": "Люди из Telegram",
            "active_nav": "settings",
            "people": await _people.list_people(user_id),
        },
    )


@router.get(
    "/settings/telegram-people/{telegram_user_id}",
    response_class=HTMLResponse,
)
async def telegram_person_page(
    telegram_user_id: int,
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    user_id = await _owner_id(session)
    detail = await _people.person_detail(user_id, telegram_user_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Telegram-пользователь не найден")
    return templates.TemplateResponse(
        request,
        "telegram_person.html",
        {
            "title": detail["person"].display_name,
            "active_nav": "settings",
            **detail,
        },
    )


@router.post("/settings/telegram-people/{telegram_user_id}")
async def telegram_person_save(
    telegram_user_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    display_name: str = Form(""),
    note: str = Form(""),
    ignored: str = Form(""),
) -> RedirectResponse:
    user_id = await _owner_id(session)
    if await _people.get_person(user_id, telegram_user_id) is None:
        raise HTTPException(status_code=404, detail="Telegram-пользователь не найден")
    await _people.set_override(
        user_id,
        telegram_user_id,
        display_name=display_name,
        note=note,
        ignored=ignored == "on",
    )
    return RedirectResponse(
        f"/settings/telegram-people/{telegram_user_id}", status_code=303
    )


@router.post("/settings/telegram-people/{telegram_user_id}/owner")
async def telegram_person_make_owner(
    telegram_user_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    user_id = await _owner_id(session)
    if await _people.get_person(user_id, telegram_user_id) is None:
        raise HTTPException(status_code=404, detail="Telegram-пользователь не найден")
    await _people.set_owner(user_id, telegram_user_id)
    return RedirectResponse(
        f"/settings/telegram-people/{telegram_user_id}", status_code=303
    )


__all__ = ["router"]
