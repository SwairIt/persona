"""T31 ФАЗА F — голосовой ассистент на Mac.

Поток:
  1. Mac-агент слушает микрофон, распознаёт речь и при wake-word «Персона …»
     шлёт фразу на ``POST /api/voice/utterance`` (авторизация — токеном агента).
  2. Сервер (если голос включён) отрезает wake-word, отправляет остаток как
     сообщение в выбранный чат (``voice_session_id``), генерирует ответ,
     кладёт его в очередь ``voice_tts``.
  3. Mac-агент опрашивает ``GET /api/voice/pending``, получает текст ответа и
     проигрывает его через macOS ``say``; затем подтверждает
     ``POST /api/voice/tts/{id}/ack``.

Захват микрофона, распознавание и ``say`` — на стороне Mac-агента
(НУЖНА ПРОВЕРКА НА MAC). Сервер хранит настройки и очередь; при выключенном
голосе все агент-эндпоинты безопасно отвечают no-op.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.remote_agents import verify_agent_token
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["voice"])
log = get_logger("persona.voice")

_WAKE_RE = re.compile(r"^\s*(персона|persona)[\s,.:!?—-]*", re.IGNORECASE | re.UNICODE)


# ---------------------------------------------------------------------------
# Settings (kv)
# ---------------------------------------------------------------------------
async def _get_settings() -> dict[str, Any]:
    async with get_connection() as conn:
        enabled = (await get_kv(conn, "voice_enabled") or "0").strip() == "1"
        sid_raw = (await get_kv(conn, "voice_session_id") or "").strip()
        name = (await get_kv(conn, "voice_name") or "").strip()
    session_id = int(sid_raw) if sid_raw.isdigit() else None
    return {"enabled": enabled, "session_id": session_id, "voice": name}


def strip_wake_word(text: str) -> str | None:
    """Вернуть фразу без wake-word, или None если wake-word отсутствует."""
    m = _WAKE_RE.match(text or "")
    if not m:
        return None
    return (text[m.end():]).strip()


# ---------------------------------------------------------------------------
# Reply generation (reuses the chat pipeline)
# ---------------------------------------------------------------------------
async def _session_owner(session_id: int) -> int | None:
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT user_id FROM chat_session WHERE id = ?", (session_id,)
        )
        row = await cur.fetchone()
    return int(row["user_id"]) if row else None


async def _generate_reply(user_id: int, session_id: int, question: str) -> str:
    """Сгенерировать ответ ассистента в указанной сессии, переиспользуя
    конвейер чата (история + системный промпт + эффорт). Персистит обе
    реплики, как обычный /send."""
    from app.chat import (  # noqa: PLC0415
        append_message,
        build_history_for_llm,
        maybe_summarise,
        touch_session,
    )
    from app.llm.client import CompletionRequest, make_client  # noqa: PLC0415
    from app.web.routes.chat_sessions import (  # noqa: PLC0415
        _base_prompt,
        _bounded_transcript,
        _EFFORT_TEMP,
        _EFFORT_TOKENS,
        _get_effort,
    )

    await append_message(session_id, "user", question)
    await touch_session(user_id, session_id)

    history = await build_history_for_llm(session_id, max_turns=20)
    if history and history[-1]["role"] == "user":
        history = history[:-1]
    transcript = _bounded_transcript(history)
    active_prompt = await _base_prompt(user_id, None)
    system = (
        f"{active_prompt}\n\nПредыдущие сообщения (для контекста):\n{transcript}"
        if transcript
        else active_prompt
    )
    # Голос — отвечаем короче: добавим подсказку (ответ будет озвучен).
    system += (
        "\n\n[Голосовой режим] Этот ответ будет ОЗВУЧЕН вслух. Отвечай кратко, "
        "простыми фразами, без markdown, списков, кода и эмодзи."
    )

    client = make_client(kind="chat")
    eff = await _get_effort(session_id)
    answer = await client.complete(
        CompletionRequest(
            system=system,
            user=question,
            temperature=_EFFORT_TEMP[eff],
            max_tokens=_EFFORT_TOKENS[eff],
        )
    )
    answer = (answer or "").strip() or "(пустой ответ)"
    await append_message(session_id, "assistant", answer)
    try:
        await maybe_summarise(session_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("voice.summary_failed", error=str(exc))
    return answer


async def _enqueue_tts(user_id: int, session_id: int | None, text: str, voice: str) -> int:
    async with get_connection() as conn:
        cur = await conn.execute(
            "INSERT INTO voice_tts (user_id, session_id, text, voice) VALUES (?, ?, ?, ?)",
            (user_id, session_id, text, voice or None),
        )
        await conn.commit()
        return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# Owner settings page
# ---------------------------------------------------------------------------
@router.get("/settings/voice", response_class=HTMLResponse)
async def voice_settings_page(
    request: Request,
    user: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    from app.chat import list_sessions  # noqa: PLC0415

    settings = await _get_settings()
    sessions = await list_sessions(user["user_id"])
    return templates.TemplateResponse(
        request,
        "voice_settings.html",
        {"settings": settings, "sessions": sessions},
    )


@router.post("/settings/voice")
async def voice_settings_save(
    request: Request,
    user: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    form = await request.form()
    enabled = "1" if form.get("enabled") else "0"
    session_id = str(form.get("session_id", "")).strip()
    voice_name = str(form.get("voice", "")).strip()
    async with get_connection() as conn:
        await set_kv(conn, "voice_enabled", enabled)
        await set_kv(conn, "voice_session_id", session_id if session_id.isdigit() else "")
        await set_kv(conn, "voice_name", voice_name)
    return RedirectResponse(url="/settings/voice", status_code=303)


# ---------------------------------------------------------------------------
# Agent-facing endpoints (bearer/X-Agent-Token auth)
# ---------------------------------------------------------------------------
async def _require_agent(authorization: str | None, x_agent_token: str | None) -> None:
    raw = ""
    if authorization and authorization.strip():
        raw = authorization.strip().removeprefix("Bearer ").strip()
    elif x_agent_token and x_agent_token.strip():
        raw = x_agent_token.strip()
    if not raw or await verify_agent_token(raw) is None:
        raise HTTPException(status_code=401, detail="invalid agent token")


@router.post("/api/voice/utterance")
async def voice_utterance(
    request: Request,
    body: Annotated[dict[str, Any], Body(...)],
    authorization: Annotated[str | None, Header()] = None,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Агент прислал распознанную фразу. Если голос включён и есть wake-word —
    генерируем ответ в выбранном чате и ставим в очередь на озвучку."""
    await _require_agent(authorization, x_agent_token)
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"ok": True, "skipped": "empty"})

    settings = await _get_settings()
    if not settings["enabled"]:
        return JSONResponse({"ok": True, "skipped": "disabled"})

    phrase = strip_wake_word(text)
    if phrase is None:
        return JSONResponse({"ok": True, "skipped": "no_wakeword"})
    if not phrase:
        return JSONResponse({"ok": True, "skipped": "wakeword_only"})

    session_id = settings["session_id"]
    if not session_id:
        return JSONResponse({"ok": True, "skipped": "no_session"})
    user_id = await _session_owner(session_id)
    if user_id is None:
        return JSONResponse({"ok": True, "skipped": "session_missing"})

    try:
        answer = await _generate_reply(user_id, session_id, phrase)
    except Exception as exc:  # noqa: BLE001
        log.warning("voice.generate_failed", error=str(exc))
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    tts_id = await _enqueue_tts(user_id, session_id, answer, settings["voice"])
    return JSONResponse(
        {"ok": True, "reply": answer, "tts_id": tts_id, "session_id": session_id}
    )


def _shell_quote(s: str) -> str:
    """Безопасно обернуть строку в одинарные кавычки для shell (для say)."""
    return "'" + s.replace("'", "'\\''") + "'"


@router.get("/api/voice/pending")
async def voice_pending(
    authorization: Annotated[str | None, Header()] = None,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Агент опрашивает очередь озвучки. Возвращает старейшую задачу или null.
    ``command`` — готовая строка ``say …`` для выполнения на Mac."""
    await _require_agent(authorization, x_agent_token)
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, text, voice FROM voice_tts WHERE status = 'pending' "
            "ORDER BY id LIMIT 1"
        )
        row = await cur.fetchone()
    if row is None:
        return JSONResponse({"pending": None})
    text = str(row["text"])
    voice = str(row["voice"] or "")
    voice_arg = f"-v {_shell_quote(voice)} " if voice else ""
    command = f"say {voice_arg}{_shell_quote(text)}"
    return JSONResponse(
        {"pending": {"id": int(row["id"]), "text": text, "voice": voice, "command": command}}
    )


@router.post("/api/voice/tts/{tts_id}/ack")
async def voice_tts_ack(
    tts_id: int,
    authorization: Annotated[str | None, Header()] = None,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Агент подтверждает, что озвучил задачу."""
    await _require_agent(authorization, x_agent_token)
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE voice_tts SET status = 'done', completed_at = datetime('now') "
            "WHERE id = ?",
            (tts_id,),
        )
        await conn.commit()
    return JSONResponse({"ok": True})
