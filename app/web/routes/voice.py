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
import tempfile
from pathlib import Path
from typing import Annotated, Any

import aiosqlite
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
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

# Озвучка: ограничиваем длину текста, уходящего в TTS, иначе огромный ответ
# (десятки КБ) превращается в десятки минут речи. Чат хранит полный ответ —
# режем только то, что отдаётся на say/SAPI.
_TTS_MAX_CHARS = 4000

# Имя голоса уходит в shell-команду `say -v …` / SAPI — допускаем только
# безопасный набор символов (буквы/цифры/пробел/подчёркивание/дефис).
_VOICE_NAME_RE = re.compile(r"^[A-Za-z0-9_\- ]*$")


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


async def _generate_reply(
    user_id: int,
    session_id: int,
    question: str,
    extra_context: str | None = None,
    include_profile: bool = True,
) -> str:
    """Сгенерировать ответ ассистента в указанной сессии, переиспользуя
    конвейер чата (история + системный промпт + эффорт). Персистит обе
    реплики, как обычный /send.

    ``extra_context`` — необязательный блок (например, кросс-чат recall),
    который подмешивается в системный промпт перед генерацией."""
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
    active_prompt = await _base_prompt(user_id, None, include_profile=include_profile)
    system = (
        f"{active_prompt}\n\nПредыдущие сообщения (для контекста):\n{transcript}"
        if transcript
        else active_prompt
    )
    if extra_context and extra_context.strip():
        system += "\n\n" + extra_context.strip()
    # Голос — отвечаем короче: добавим подсказку (ответ будет озвучен).
    system += (
        "\n\n[Голосовой режим] Этот ответ будет ОЗВУЧЕН вслух. Отвечай кратко, "
        "простыми фразами, без markdown, списков, кода и эмодзи."
    )

    client = make_client(kind="chat", user_id=int(user_id))
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

    from app.voice_config import (  # noqa: PLC0415
        STT_ENGINES,
        TTS_ENGINES,
        get_voice_config,
    )

    settings = await _get_settings()
    sessions = await list_sessions(user["user_id"])
    return templates.TemplateResponse(
        request,
        "voice_settings.html",
        {
            "settings": settings,
            "sessions": sessions,
            "engine": await get_voice_config(),
            "stt_engines": STT_ENGINES,
            "tts_engines": TTS_ENGINES,
        },
    )


@router.post("/settings/voice")
async def voice_settings_save(
    request: Request,
    user: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    form = await request.form()
    enabled = "1" if form.get("enabled") else "0"
    session_id = str(form.get("session_id", "")).strip()
    # Имя голоса уходит в shell `say -v …` / SAPI — жёстко санитизируем:
    # обрезаем до 50 символов и разрешаем только безопасный набор символов.
    # Кавычки/escape/любой посторонний символ → имя сбрасывается в пустое
    # (агент возьмёт дефолтный голос), а не уходит в команду.
    voice_name = str(form.get("voice", "")).strip()[:50]
    if not _VOICE_NAME_RE.match(voice_name):
        log.warning("voice.invalid_voice_name", value=voice_name)
        voice_name = ""
    async with get_connection() as conn:
        await set_kv(conn, "voice_enabled", enabled)
        await set_kv(conn, "voice_session_id", session_id if session_id.isdigit() else "")
        await set_kv(conn, "voice_name", voice_name)
    # S4c — конфиг движков (применяет агент на устройстве); сервер валидирует/хранит.
    from app.voice_config import save_voice_config  # noqa: PLC0415

    await save_voice_config(
        {
            "stt_engine": form.get("stt_engine"),
            "vad_enabled": form.get("vad_enabled"),
            "vad_threshold": form.get("vad_threshold"),
            "tts_engine": form.get("tts_engine"),
            "tts_voice": form.get("tts_voice"),
            "tts_rate": form.get("tts_rate"),
            "barge_in": form.get("barge_in"),
        }
    )
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
    except aiosqlite.IntegrityError as exc:
        # FK-гонка: сессию удалили между owner-check и записью ответа.
        # Не падаем в 502 — это ожидаемое состояние, агент просто пропустит.
        log.warning("voice.session_deleted", session_id=session_id, error=str(exc))
        return JSONResponse(
            {"ok": False, "skipped": "session_deleted"}, status_code=410
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("voice.generate_failed", error=str(exc))
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    # TTS-длина: режем то, что уходит в озвучку (чат уже хранит полный ответ).
    if len(answer) > _TTS_MAX_CHARS:
        log.warning(
            "voice.tts_truncated",
            session_id=session_id,
            original_len=len(answer),
            limit=_TTS_MAX_CHARS,
        )
        answer = answer[:_TTS_MAX_CHARS]

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


@router.get("/api/voice/config")
async def voice_config(
    authorization: Annotated[str | None, Header()] = None,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Агент тянет конфиг голосовых движков (STT/VAD/TTS/barge-in) и применяет
    его НА УСТРОЙСТВЕ. Сервер только хранит/валидирует предпочтения."""
    await _require_agent(authorization, x_agent_token)
    from app.voice_config import get_voice_config  # noqa: PLC0415

    base = await _get_settings()
    cfg = await get_voice_config()
    cfg["enabled"] = base["enabled"]
    cfg["voice"] = base["voice"]  # обратная совместимость со старым say -v
    return JSONResponse(cfg)


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


# ---------------------------------------------------------------------------
# Браузерный STT — серверный fallback к Web Speech API
# ---------------------------------------------------------------------------
_STT_MAX_BYTES = 12 * 1024 * 1024  # ~12 МБ хватает на длинную фразу


@router.post("/api/voice/web/stt")
async def voice_web_stt(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    file: Annotated[UploadFile, File(...)],
    locale: str = "",
) -> JSONResponse:
    """Распознать аудио-блоб из браузера (микрофон чата) → текст.

    Fallback для браузеров без Web Speech API (или когда он недоступен):
    клиент пишет звук через MediaRecorder и шлёт сюда. Транскрипция —
    тем же серверным Whisper, что и для голосовых заметок
    (``app.audio.transcribe.transcribe_segment``). Если бэкенд Whisper не
    установлен — 503 (клиент тогда подсказывает «используй Chrome»).
    """
    from app.audio.transcribe import transcribe_segment  # noqa: PLC0415

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="пустой аудио-блоб")
    if len(raw) > _STT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="слишком большой аудио-блоб")

    # MediaRecorder отдаёт webm/ogg/mp4 — Whisper (ffmpeg) понимает их по
    # расширению. Берём суффикс из mime, дефолт webm.
    mime = (file.content_type or "").lower()
    suffix = ".webm"
    if "ogg" in mime:
        suffix = ".ogg"
    elif "mp4" in mime or "m4a" in mime:
        suffix = ".m4a"
    elif "wav" in mime:
        suffix = ".wav"

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)
        text = await transcribe_segment(tmp_path, locale_hint=(locale or None))
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("voice.web_stt.failed", error=str(exc))
        return JSONResponse(
            {"ok": False, "error": "transcribe_failed"}, status_code=500
        )
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    if text is None:
        # Бэкенд Whisper не установлен на сервере.
        return JSONResponse(
            {"ok": False, "error": "no_backend",
             "hint": "Whisper не установлен на сервере — используй Chrome (Web Speech) для голосового ввода"},
            status_code=503,
        )
    return JSONResponse({"ok": True, "text": text})


# ---------------------------------------------------------------------------
# Контекст сессии для fullscreen /voice — чтобы юзер не «говорил вслепую»
# ---------------------------------------------------------------------------
@router.get("/api/voice/web/context")
async def voice_web_context(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    session_id: int = 0,
    limit: int = 3,
) -> JSONResponse:
    """Лёгкий контекст разговора для орб-страницы /voice.

    Возвращает заголовок выбранной сессии, закреплённые на ней provider/model
    (чтобы пикер модели показывал состояние ИМЕННО этой сессии) и последние
    ``limit`` реплик диалога — фронт рисует их над орбом, чтобы было видно
    контекст. Best-effort: при отсутствии/чужой сессии — пустой ответ, а не
    ошибка (страница работает и без контекста).
    """
    from app.chat import get_session, list_messages  # noqa: PLC0415

    # 2–4 реплики достаточно для контекста; жёстко ограничиваем сверху.
    take = max(1, min(8, int(limit) if limit else 3))

    # session_id=0 → берём самую свежую сессию пользователя.
    sid = int(session_id) if session_id else 0
    thread = None
    if sid:
        thread = await get_session(session["user_id"], sid)
    else:
        from app.chat import list_sessions  # noqa: PLC0415

        recent = await list_sessions(session["user_id"], limit=1)
        if recent:
            thread = recent[0]
            sid = int(thread["id"])

    if thread is None:
        return JSONResponse(
            {"ok": True, "session_id": None, "title": None, "messages": []}
        )

    # Последние ``take`` реплик в хронологическом порядке (старые → новые).
    rows = await list_messages(sid, limit=500)
    tail = rows[-take:] if len(rows) > take else rows

    def _preview(text: str, limit: int = 160) -> str:
        # Превью-строка над орбом: одна строка, без переносов, обрезаем длинное.
        s = " ".join(str(text).split())
        return (s[: limit - 1] + "…") if len(s) > limit else s

    messages = [
        {"role": str(m["role"]), "text": _preview(m["content"])}
        for m in tail
        if m["role"] in ("user", "assistant")
    ]
    return JSONResponse(
        {
            "ok": True,
            "session_id": sid,
            "title": str(thread["title"]),
            "provider": thread["provider"],
            "model": thread["model"],
            "messages": messages,
        }
    )
