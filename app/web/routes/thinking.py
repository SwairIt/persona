"""Owner-only surface for Persona's self-directed thinking loop (v2.30.21).

Two pages:

- ``/settings/thinking`` — turn the loop on, tune its caps/budget/model and
  which seed kinds it may draw on. Follows the read-with-fallback GET /
  strict-validate POST shape of :mod:`app.web.routes.theme`.
- ``/thoughts`` — the diary: chains newest-first with their steps, so the
  owner can actually read what she thought.

The confirm action (``POST /thoughts/{id}/confirm``) is the ONLY place a
thought becomes a remembered fact, and it lives here deliberately:
``app/thinking`` has no write path into memory at all (see
``tests/test_thinking_no_memory_writes.py``), so promotion has to happen at
the web-route layer, on an explicit owner click, calling
:func:`app.chat.user_memory.add_memory` directly. ``ThoughtStore.mark_confirmed``
guards against promoting the same conclusion twice.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.chat.user_memory import add_memory
from app.llm.worker_queue import worker_status
from app.logging_setup import get_logger
from app.thinking.settings import (
    ALL_SEED_KINDS,
    ThinkingSettings,
    load_thinking_settings,
    save_thinking_settings,
)
from app.thinking.store import ThoughtStore
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.thinking.web")
_store = ThoughtStore()


async def _owner_id(session: SessionRecord) -> int:
    user_id = int(session["user_id"])
    if not await is_owner(user_id):
        raise HTTPException(status_code=403, detail="Только владелец")
    return user_id


@router.get("/settings/thinking", response_class=HTMLResponse)
async def thinking_settings_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    user_id = await _owner_id(session)
    settings = await load_thinking_settings()
    steps_used_today = await _store.steps_used_today(user_id)
    open_chain = await _store.oldest_open_chain(user_id)
    worker_model = (await worker_status()).get("model") or ""
    return templates.TemplateResponse(
        request,
        "thinking_settings.html",
        {
            "title": "Мышление",
            "active_nav": "settings",
            "settings": settings,
            "all_seed_kinds": ALL_SEED_KINDS,
            "steps_used_today": steps_used_today,
            "open_chain": open_chain,
            "worker_model": worker_model or "",
        },
    )


@router.post("/settings/thinking")
async def thinking_settings_save(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    enabled: str = Form(""),
    cap_mode: str = Form("fixed"),
    step_cap: int = Form(5),
    emergency_cap: int = Form(50),
    daily_budget: int = Form(60),
    may_write_to_chat: str = Form(""),
    model: str = Form(""),
    seed_kinds: list[str] = Form([]),
    quiet_minutes: int = Form(3),
) -> RedirectResponse:
    await _owner_id(session)

    new_settings = ThinkingSettings(
        enabled=enabled == "on",
        cap_mode=cap_mode.strip().lower(),
        step_cap=step_cap,
        emergency_cap=emergency_cap,
        daily_budget=daily_budget,
        seed_kinds=tuple(seed_kinds),
        may_write_to_chat=may_write_to_chat == "on",
        model=model.strip(),
        quiet_minutes=quiet_minutes,
    )
    try:
        await save_thinking_settings(new_settings)
    except ValueError as exc:
        log.warning("thinking.settings.rejected", error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log.info("thinking.settings.saved", enabled=new_settings.enabled)
    return RedirectResponse(url="/settings/thinking", status_code=303)


@router.get("/thoughts", response_class=HTMLResponse)
async def thoughts_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    user_id = await _owner_id(session)
    chains = await _store.recent_chains(user_id)
    for chain in chains:
        chain["steps"] = await _store.chain_steps(chain["chain_id"])
    return templates.TemplateResponse(
        request,
        "thoughts.html",
        {
            "title": "Дневник мыслей",
            "active_nav": "settings",
            "chains": chains,
        },
    )


@router.post("/thoughts/{thought_id}/confirm")
async def thoughts_confirm(
    thought_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    user_id = await _owner_id(session)
    thought = await _store.get_thought(thought_id)
    if (
        thought is None
        or int(thought["persona_user_id"]) != user_id
        or thought["kind"] != "conclusion"
    ):
        raise HTTPException(status_code=404, detail="Мысль не найдена")

    if thought["confirmed_at"] is None:
        # The ONLY write path from a thought into memory: an explicit owner
        # click on ONE conclusion, done here in the web-route layer.
        # app/thinking itself never calls add_memory — see
        # tests/test_thinking_no_memory_writes.py.
        await add_memory(user_id, thought["text"], kind="fact", pinned=True)
        await _store.mark_confirmed(thought_id)
        log.info("thinking.thought.confirmed", thought_id=thought_id)

    return RedirectResponse(url="/thoughts", status_code=303)


__all__ = ["router"]
