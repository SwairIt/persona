"""Дашборд приватности — /settings/privacy. Страница ВЛАДЕЛЬЦА инстанса.

Приватность как ВИДИМАЯ, проверяемая фича (главный вакуум после ухода Rewind в
Meta): что хранится локально, какой провайдер активен (локальный Ollama vs
внешнее облако), экспорт ВСЕЙ памяти (Markdown + снимок БД), удаление всего.

ОБЛАСТЬ ДЕЙСТВИЯ (важно)
────────────────────────
Здесь инстанс-глобальные рычаги, а не «мои данные». В частности
:func:`db_snapshot` делает ``VACUUM INTO`` по **всей базе**: чаты всех
аккаунтов, скриншоты и OCR владельца, kv-секреты (SMTP-пароль, токен
Telegram-бота, ключи API). Такой артефакт нельзя отдать участнику ни при
каком фильтре — он глобален по своей природе.

Поэтому ``/settings/privacy`` НЕ входит в ``_MEMBER_PREFIXES``, а участнику
сделан отдельный экран ``/settings/my-data``
(:mod:`app.web.routes.my_data`) — там всё строго per-user.

Опасные подроуты вдобавок закрыты ЯВНОЙ owner-зависимостью
(:func:`_require_owner`): гейт — не единственный рубеж, и если префикс когда-то
откроют по недосмотру, снимок базы всё равно не уедет.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.member_crypto import decrypt_for_user
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.privacy")


async def _require_owner(user_id: int, action: str) -> None:
    """403, если вызывающий не владелец инстанса. Защита в глубину.

    Резолв владельца — fail-closed (``app/web/routes/owner_view.viewer_is_owner``
    трактует сбой как «не владелец»), поэтому упавшая БД не открывает снимок.
    """
    from app.web.routes.owner_view import viewer_is_owner  # noqa: PLC0415

    if not await viewer_is_owner(int(user_id)):
        log.warning("privacy.owner_only_denied", user_id=int(user_id), action=action)
        raise HTTPException(
            status_code=403,
            detail=(
                "Это инструмент владельца инстанса. Свои данные — "
                "на /settings/my-data."
            ),
        )

# Локальные провайдеры (данные не покидают машину). Остальные — внешнее облако.
_LOCAL_PROVIDERS = {"ollama", "llamacpp", "localai", "lmstudio"}


async def _provider() -> str:
    async with get_connection() as conn:
        return (await get_kv(conn, "llm_provider") or "ollama").strip().lower()


async def _counts(user_id: int) -> dict[str, int]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM user_memory WHERE user_id = ? AND valid_until IS NULL",
            (user_id,),
        )
        facts = int((await cur.fetchone())["n"])
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM chat_message m JOIN chat_session s ON s.id = m.session_id "
            "WHERE s.user_id = ?",
            (user_id,),
        )
        msgs = int((await cur.fetchone())["n"])
    return {"facts": facts, "messages": msgs}


async def _export_markdown(user_id: int) -> str:
    """Вся память пользователя (факты + чаты) в Markdown — портативность/анти-lock-in."""
    out: list[str] = ["# Persona — экспорт памяти\n"]
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT kind, text, pinned, created_at, valid_until FROM user_memory "
            "WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        facts = await cur.fetchall()
        out.append(f"\n## Факты о пользователе ({len(facts)})\n")
        for f in facts:
            mark = "📌 " if f["pinned"] else "- "
            stale = " _(устарело)_" if f["valid_until"] else ""
            # У не-владельца текст факта лежит зашифрованным — экспорт обязан
            # отдать человеку читаемое, иначе «портативность» превращается в
            # выгрузку мусора.
            plain = await decrypt_for_user(int(user_id), f["text"], conn)
            out.append(f"{mark}[{f['kind']}] {plain}{stale}")
        cur = await conn.execute(
            "SELECT id, title, created_at FROM chat_session WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        sessions = await cur.fetchall()
        out.append(f"\n## Чаты ({len(sessions)})\n")
        for s in sessions:
            out.append(f"\n### {s['title'] or ('чат #' + str(s['id']))}  ({(s['created_at'] or '')[:10]})\n")
            mcur = await conn.execute(
                "SELECT role, content, created_at FROM chat_message "
                "WHERE session_id = ? AND is_streaming = 0 ORDER BY id",
                (s["id"],),
            )
            for m in await mcur.fetchall():
                who = "**Ты**" if m["role"] == "user" else ("**Persona**" if m["role"] == "assistant" else "_sys_")
                out.append(f"{who}: {(m['content'] or '').strip()}\n")
    return "\n".join(out)


@router.get("/settings/privacy", response_class=HTMLResponse)
async def privacy_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Страница владельца. Участнику — 403 и подсказка на его собственный экран."""
    await _require_owner(int(session["user_id"]), "page")
    provider = await _provider()
    return templates.TemplateResponse(
        request,
        "privacy_settings.html",
        {
            "title": "Приватность — твои данные",
            "active_nav": "settings",
            "provider": provider,
            "is_local": provider in _LOCAL_PROVIDERS,
            "counts": await _counts(session["user_id"]),
        },
    )


@router.get("/settings/privacy/export-memory")
async def export_memory(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> Response:
    md = await _export_markdown(session["user_id"])
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="persona-memory.md"'},
    )


@router.get("/settings/privacy/snapshot")
async def db_snapshot(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> FileResponse:
    """Снимок БД через VACUUM INTO (без блокировки рабочей БД) → скачать.

    OWNER-ONLY, и это не косметика: файл содержит ВСЮ базу инстанса —
    чаты всех аккаунтов, личные данные владельца и kv-секреты. Участник свою
    выгрузку берёт на ``/settings/my-data/export.json`` (там каждый запрос
    отфильтрован по его ``user_id``).
    """
    await _require_owner(int(session["user_id"]), "snapshot")
    from app.settings import get_settings  # noqa: PLC0415

    src = str(get_settings().db_path)
    tmp = os.path.join(tempfile.gettempdir(), f"persona-snapshot-{session['user_id']}.db")
    if os.path.exists(tmp):
        try:
            os.unlink(tmp)
        except OSError:
            pass

    def _vacuum() -> None:
        con = sqlite3.connect(src)
        try:
            con.execute("VACUUM INTO ?", (tmp,))
        finally:
            con.close()

    await asyncio.to_thread(_vacuum)
    return FileResponse(tmp, media_type="application/octet-stream", filename="persona-snapshot.db")


@router.post("/settings/privacy/wipe-memory", response_model=None)
async def wipe_memory(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    confirm: str = Form(default=""),
) -> RedirectResponse:
    """Удалить ВСЮ личную память (факты). typed-confirm = «УДАЛИТЬ»."""
    if confirm.strip().upper() == "УДАЛИТЬ":
        from app.storage.db import write_transaction  # noqa: PLC0415

        async with write_transaction() as conn:
            await conn.execute("DELETE FROM user_memory WHERE user_id = ?", (session["user_id"],))
        log.info("privacy.wipe_memory", user_id=session["user_id"])
    return RedirectResponse("/settings/privacy", status_code=303)


__all__ = ["router"]
