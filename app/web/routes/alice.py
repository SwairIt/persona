"""Навык Алисы (Яндекс.Диалоги) → Персона.

«Алиса, открой Персону» → каждая реплика приходит сюда вебхуком, уходит в
чат-движок Персоны (recall + граф + LLM, по умолчанию YandexGPT), сохраняется
в БД как обычные сообщения чата (→ память, граф памяти и умный поиск
индексируют их сами) и озвучивается Алисой.

Вебхук ПУБЛИЧНЫЙ (Яндекс зовёт из интернета), защищён секретом в пути:
``POST /api/alice/webhook/{token}`` сверяется с kv ``alice_webhook_secret``.

Ограничение платформы: Алиса ждёт ответ ~2–4 c. Поэтому мозг — быстрый
YandexGPT, а генерация под ``asyncio.shield`` — если не уложились в таймаут,
Алисе уходит «секунду…», но ответ всё равно допишется в память фоном.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.owner import get_owner_user_id
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["alice"])
log = get_logger("persona.alice")

_GEN_TIMEOUT = 3.2  # сек — укладываемся в лимит ответа Алисы
_GREETING = (
    "Привет! Я Персона — твоя память. Спрашивай что угодно: "
    "я помню наши прошлые разговоры."
)
_EXITS = {"хватит", "выход", "стоп", "пока", "закрой", "выключись"}


async def _alice_session_id(owner_id: int) -> int:
    """Найти/создать выделенную сессию чата для Алисы (kv alice_session_id)."""
    async with get_connection() as conn:
        raw = (await get_kv(conn, "alice_session_id") or "").strip()
        if raw.isdigit():
            cur = await conn.execute(
                "SELECT id FROM chat_session WHERE id = ? AND user_id = ?",
                (int(raw), owner_id),
            )
            if await cur.fetchone():
                return int(raw)
    from app.chat.sessions import create_session  # noqa: PLC0415

    sess = await create_session(owner_id, title="Алиса (голос)")
    async with get_connection() as conn:
        await set_kv(conn, "alice_session_id", str(sess.id))
    return sess.id


def _reply(text: str, *, end: bool = False, version: str = "1.0") -> JSONResponse:
    text = (text or "").strip()[:1024] or "…"
    return JSONResponse(
        {
            "version": version,
            "response": {"text": text, "tts": text, "end_session": end},
        }
    )


@router.post("/api/alice/webhook/{token}")
async def alice_webhook(
    token: str,
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """Точка входа навыка Алисы. Защищена секретом в пути (token)."""
    version = str(body.get("version") or "1.0")

    async with get_connection() as conn:
        secret = (await get_kv(conn, "alice_webhook_secret") or "").strip()
    if not secret:
        return _reply(
            "Навык ещё не настроен в Персоне. Открой настройки Алисы и задай секрет.",
            end=True,
            version=version,
        )
    if not secrets.compare_digest(token, secret):
        log.warning("alice.bad_token")
        return _reply("Доступ запрещён.", end=True, version=version)

    req = body.get("request") or {}
    session = body.get("session") or {}
    command = str(req.get("command") or req.get("original_utterance") or "").strip()
    is_new = bool(session.get("new"))

    if is_new and not command:
        return _reply(_GREETING, version=version)
    if command.lower().strip(" .!?,") in _EXITS:
        return _reply("Пока! Я всё запомнила.", end=True, version=version)
    if not command:
        return _reply("Я слушаю. Спроси что-нибудь.", version=version)

    owner_id = await get_owner_user_id()
    if owner_id is None:
        return _reply(
            "В Персоне ещё нет владельца — заведи аккаунт.", end=True, version=version
        )

    try:
        sid = await _alice_session_id(owner_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("alice.session_failed", error=str(exc))
        return _reply("Не удалось открыть память, попробуй ещё раз.", version=version)

    # Кросс-чат recall (быстрый keyword) для «помнит всё».
    recall = ""
    try:
        from app.chat.sessions import recall_relevant  # noqa: PLC0415

        recall = await recall_relevant(
            owner_id, command, exclude_session_id=sid, limit=5
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("alice.recall_failed", error=str(exc))

    from app.web.routes.voice import _generate_reply  # noqa: PLC0415

    extra = (
        "Релевантная память из других бесед (используй, если уместно):\n" + recall
        if recall.strip()
        else None
    )
    # shield: ответ допишется в БД даже если мы вернём заглушку по таймауту.
    task = asyncio.ensure_future(
        _generate_reply(owner_id, sid, command, extra_context=extra)
    )
    try:
        answer = await asyncio.wait_for(asyncio.shield(task), timeout=_GEN_TIMEOUT)
    except asyncio.TimeoutError:
        answer = "Секунду, я ещё вспоминаю. Повтори вопрос, пожалуйста."
    except Exception as exc:  # noqa: BLE001
        log.warning("alice.generate_failed", error=str(exc))
        answer = "Что-то пошло не так, попробуй ещё раз."
    return _reply(answer, version=version)


# ---------------------------------------------------------------------------
# Настройки (owner-only; /settings/* закрыт auth-gate для не-владельца)
# ---------------------------------------------------------------------------
@router.get("/settings/alice", response_class=HTMLResponse)
async def alice_settings(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    async with get_connection() as conn:
        secret = (await get_kv(conn, "alice_webhook_secret") or "").strip()
    base = str(request.base_url).rstrip("/")
    webhook = f"{base}/api/alice/webhook/{secret}" if secret else ""
    return templates.TemplateResponse(
        request,
        "alice_settings.html",
        {
            "title": "Алиса → Персона",
            "active_nav": "settings",
            "secret": secret,
            "webhook_url": webhook,
        },
    )


@router.post("/settings/alice", response_class=HTMLResponse)
async def alice_settings_save(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    form = await request.form()
    secret = str(form.get("secret") or "").strip()
    if str(form.get("generate") or "") == "1" or not secret:
        secret = secrets.token_urlsafe(18)
    # секрет — только URL-безопасные символы (он живёт в пути вебхука)
    secret = "".join(ch for ch in secret if ch.isalnum() or ch in "-_")[:64]
    async with get_connection() as conn:
        await set_kv(conn, "alice_webhook_secret", secret)
    return RedirectResponse("/settings/alice", status_code=303)
