"""HTTP surface for persistent chat sessions.

Pairs with ``app/chat/sessions.py``. Two flavours:

  * **HTML**: ``/chat/sessions`` lists threads, ``/chat/{id}`` opens one.
  * **JSON**: ``/api/chat/sessions``, ``/api/chat/sessions/{id}/messages``,
    ``/api/chat/sessions/{id}/send`` — the last one is the workhorse:
    it accepts a user message, calls the LLM with the session's full
    history pre-pended, persists both the user turn and the assistant
    response, and returns the assistant text + the new session/message
    ids.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.chat import (
    add_span_rating,
    append_message,
    build_history_for_llm,
    create_session,
    delete_session,
    finalize_streaming_message,
    get_active_system_prompt,
    get_pinned_messages,
    get_session,
    get_span_ratings,
    latest_reaction,
    get_streaming_message,
    list_messages,
    list_sessions,
    maybe_summarise,
    rename_session,
    search_messages,
    set_message_pinned,
    set_reaction,
    start_streaming_message,
    touch_session,
    update_session_model,
    update_streaming_message,
)
from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["chat"])
log = get_logger("persona.chat.routes")


async def _find_vision_model_for_provider(provider: str | None) -> str | None:
    """T24 — return the first vision-capable installed model for the
    given provider, or None if none available. Used by auto-switch when
    user attaches an image but is currently on a text-only model."""
    if not provider:
        return None
    if provider != "ollama":
        # For cloud providers we don't auto-swap; their picker default
        # is usually multimodal (gpt-4o, gemini, claude all see images).
        return None
    from app.storage.db import get_connection  # noqa: PLC0415
    from app.storage.repository import get_kv  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    async with get_connection() as conn:
        endpoint = (await get_kv(conn, "byo_api_key_ollama") or "").strip()
    endpoint = endpoint or "http://localhost:11434"
    try:
        async with httpx.AsyncClient(timeout=4.0) as cli:
            resp = await cli.get(endpoint.rstrip("/") + "/api/tags")
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        return None
    for m in data.get("models", []):
        name = str(m.get("name", ""))
        if any(kw in name.lower() for kw in ("vl", "vision", "llava", "moondream")):
            return name
    return None

# T29 — the default chat prompt now lives in app/chat/prompts.py
# (DEFAULT_SYSTEM_PROMPT) and is served via get_active_system_prompt(), so
# the user can pick/edit presets. The old hard-coded _SYSTEM_PROMPT_RU was
# removed to avoid a stale duplicate.

_SYSTEM_PROMPT_VISION = (
    "Ты — личный AI с компьютерным зрением. К сообщению прикреплено "
    "изображение — рассмотри и опиши что видишь. Будь точным; если "
    "что-то нечитаемо — скажи. Не отказывайся от описания. Не ври. "
    "Отвечай по-русски."
)


# T31 — идентичность: ИИ знает, что он Persona. Всегда в начале промпта.
_PERSONA_IDENTITY = (
    "Ты — Persona: персональный ИИ этого пользователя (модель называется "
    "«Persona»). Если спросят, кто ты или какая ты модель — ты Persona, "
    "личный ассистент с памятью о пользователе, работающий на его стороне. "
    "Не называй себя другим именем или чужой моделью.\n\n"
)

# T31 E8 — меню выбора ответов (как у Клода). ИИ может в КОНЦЕ сообщения
# добавить блок выбора; фронт превратит его в кнопки над полем ввода.
_CHOICES_HINT = (
    "\n\nМеню выбора: когда уместно предложить пользователю выбрать "
    "направление/вариант (а не строчить простыню), добавь В САМОМ КОНЦЕ "
    "сообщения отдельный блок (ровно такой формат, на отдельных строках):\n"
    "```persona:choices\n"
    '{"question": "краткий вопрос", "options": ['
    '{"label": "Вариант 1", "desc": "пояснение", "recommended": true}, '
    '{"label": "Вариант 2", "desc": "пояснение"}]}\n'
    "```\n"
    "Правила: 2–4 варианта; ровно один recommended:true; label короткий "
    "(до ~5 слов); валидный JSON; блок ОДИН и только в конце. Не злоупотребляй "
    "— только когда выбор реально экономит время. Пользователь нажмёт вариант "
    "или впишет свой."
)


async def _base_prompt(user_id: int, image_data_url: str | None) -> str:
    """T29 — the system prompt for a turn: vision prompt (if image) or the
    user's active prompt, PLUS the user's 'about me' profile so the AI knows
    who it's talking to. T31 — также префикс идентичности Persona.
    Used by all chat paths (send/send-stream/compare)."""
    from app.profile import get_profile, profile_block  # noqa: PLC0415

    base = _SYSTEM_PROMPT_VISION if image_data_url else await get_active_system_prompt()
    return _PERSONA_IDENTITY + base + _CHOICES_HINT + profile_block(await get_profile(user_id))


# T31 E2 — эффорт: бюджет ответа (max_tokens) + температура. Гибрид «мощности».
_EFFORT_TOKENS: dict[str, int] = {"fast": 900, "normal": 4096, "deep": 8192}
_EFFORT_TEMP: dict[str, float] = {"fast": 0.5, "normal": 0.7, "deep": 0.7}


async def _get_effort(session_id: int) -> str:
    from app.storage.db import get_connection  # noqa: PLC0415
    from app.storage.repository import get_kv  # noqa: PLC0415

    async with get_connection() as conn:
        v = (await get_kv(conn, f"chat_effort_{session_id}") or "").strip()
    return v if v in _EFFORT_TOKENS else "normal"


async def _set_effort(session_id: int, effort: str) -> None:
    from app.storage.db import get_connection  # noqa: PLC0415
    from app.storage.repository import set_kv  # noqa: PLC0415

    async with get_connection() as conn:
        await set_kv(conn, f"chat_effort_{session_id}", effort)
        await conn.commit()


# T31 E3 — режимы работы. Инструменты выполняются только в auto/bypass.
_MODES: tuple[str, ...] = ("plan", "ask", "auto", "bypass")
_MODE_HINT: dict[str, str] = {
    "plan": (
        "\n\nРЕЖИМ: ПЛАН. Только составь подробный план действий (что бы ты "
        "сделал по шагам). НЕ выполняй инструменты, НЕ пиши вызовы <tool>. "
        "Заверши предложением подтвердить план."
    ),
    "ask": (
        "\n\nРЕЖИМ: СПРАШИВАТЬ. Прежде чем что-то делать (создавать файлы, "
        "запускать команды) — СПРОСИ разрешение: предложи действие через блок "
        "```persona:choices``` («Сделать X?» с вариантами Да/Нет) и дождись "
        "ответа. Сам инструменты не выполняй."
    ),
    "auto": "\n\nРЕЖИМ: АВТО. Действуй сам, вызывай инструменты где нужно.",
    "bypass": (
        "\n\nРЕЖИМ: БЕЗ СПРОСА. Выполняй задачу до конца, не переспрашивай по "
        "мелочам, используй инструменты свободно."
    ),
}


async def _get_mode(session_id: int) -> str:
    from app.storage.db import get_connection  # noqa: PLC0415
    from app.storage.repository import get_kv  # noqa: PLC0415

    async with get_connection() as conn:
        v = (await get_kv(conn, f"chat_mode_{session_id}") or "").strip()
    return v if v in _MODES else "auto"


async def _set_mode(session_id: int, mode: str) -> None:
    from app.storage.db import get_connection  # noqa: PLC0415
    from app.storage.repository import set_kv  # noqa: PLC0415

    async with get_connection() as conn:
        await set_kv(conn, f"chat_mode_{session_id}", mode)
        await conn.commit()


class _LiveGen:
    """T29 — one in-flight chat generation, decoupled from the HTTP client.

    The generation runs in a DETACHED task that emits SSE frames here;
    the HTTP response just relays them. If the user closes the page the
    relay is cancelled but the generation task keeps running to the end
    (incl. tool calls + persisting the assistant message), so nothing the
    model did is lost. Connected clients subscribe and get a replay of
    everything emitted so far.
    """

    def __init__(self) -> None:
        self.buffer: list[str] = []
        self.subscribers: list[asyncio.Queue] = []
        self.done = False
        self.task: asyncio.Task | None = None

    def emit(self, frame: str) -> None:
        self.buffer.append(frame)
        for q in list(self.subscribers):
            q.put_nowait(frame)

    def finish(self) -> None:
        self.done = True
        for q in list(self.subscribers):
            q.put_nowait(None)  # sentinel = end of stream

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for f in self.buffer:  # replay so a (re)connecting client catches up
            q.put_nowait(f)
        if self.done:
            q.put_nowait(None)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self.subscribers.remove(q)
        except ValueError:
            pass


# session_id → active generation. Lets a reopened page attach to a run
# that's still going, and keeps the run alive when no client is attached.
_LIVE_GENS: dict[int, _LiveGen] = {}


# T29 — cap how much chat history we send by CHARACTERS, not turn count: a
# few huge turns (code, tool output) of 20 turns could still fill the whole
# context window and starve the answer (input 16383/16384 → 1-token reply).
# Keep the MOST RECENT turns that fit; older context lives in the summary.
_HISTORY_CHAR_BUDGET = 28000  # ~7k tokens; leaves room for prompt + answer


def _bounded_transcript(
    history: list[dict[str, str]], budget: int = _HISTORY_CHAR_BUDGET
) -> str:
    kept: list[str] = []
    total = 0
    for turn in reversed(history):
        line = f"[{turn['role']}] {turn['content']}"
        if kept and total + len(line) > budget:
            break
        kept.append(line)
        total += len(line)
    return "\n".join(reversed(kept))


# --- HTML pages ------------------------------------------------------------


@router.get("/chat", response_class=HTMLResponse, response_model=None)
async def chat_index(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse | RedirectResponse:
    """Sidebar + welcome screen. Opens the most recent thread if one
    exists, else shows a 'new chat' splash."""
    sessions = await list_sessions(session["user_id"], limit=50)
    if sessions:
        return RedirectResponse(url=f"/chat/{sessions[0]['id']}", status_code=303)
    return templates.TemplateResponse(
        request,
        "chat_index.html",
        {
            "title": "Чат с памятью",
            "active_nav": "ask",
            "sessions": sessions,
            "active_session": None,
            "messages": [],
        },
    )


@router.get("/chat/{session_id}", response_class=HTMLResponse, response_model=None)
async def chat_thread(
    request: Request,
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Open one conversation thread."""
    thread = await get_session(session["user_id"], session_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    sessions = await list_sessions(session["user_id"], limit=50)
    messages = await list_messages(session_id, limit=500)
    # T31 — подтянуть спан-рейтинги (подсветка выделенных фрагментов).
    spans = await get_span_ratings(session_id)
    for m in messages:
        m["span_ratings"] = spans.get(int(m["id"]), [])
    effort = await _get_effort(session_id)
    mode = await _get_mode(session_id)
    return templates.TemplateResponse(
        request,
        "chat_index.html",
        {
            "title": thread["title"],
            "active_nav": "ask",
            "sessions": sessions,
            "active_session": thread,
            "messages": messages,
            "effort": effort,
            "mode": mode,
        },
    )


# --- JSON API --------------------------------------------------------------


@router.get("/api/chat/sessions", response_class=JSONResponse)
async def api_list_sessions(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    rows = await list_sessions(session["user_id"], limit=50)
    return JSONResponse({"sessions": rows})


@router.post("/api/chat/sessions", response_class=JSONResponse)
async def api_create_session(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    title = (
        str(body.get("title") or "").strip() if isinstance(body, dict) else ""
    )
    row = await create_session(session["user_id"], title=title or None)
    return JSONResponse(row, status_code=201)


@router.get("/api/chat/sessions/{session_id}/messages", response_class=JSONResponse)
async def api_list_messages(
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    thread = await get_session(session["user_id"], session_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    rows = await list_messages(session_id, limit=500)
    return JSONResponse({"session": thread, "messages": rows})


@router.get("/api/chat/search", response_class=JSONResponse)
async def api_chat_search(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    q: str = "",
) -> JSONResponse:
    """T29 — search across all of the user's chat messages for the in-UI
    search box. Returns recent-first matches with session title + excerpt."""
    results = await search_messages(session["user_id"], q, limit=40)
    return JSONResponse({"results": results})


_BUILD_FILES_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        }
    },
    "required": ["files"],
}


@router.post("/api/chat/sessions/{session_id}/build", response_class=JSONResponse)
async def api_build_files(
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """T29 — RELIABLE file creation. Instead of hoping the model emits a
    correct <tool>write_file</tool>, we constrain it to a JSON schema
    (structured output) and write the files ourselves. Guaranteed even on a
    weak 7B. Files land in the workspace (→ sync to the Mac agent)."""
    thread = await get_session(session["user_id"], session_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    spec = str(body.get("prompt", "")).strip()
    if not spec:
        raise HTTPException(status_code=400, detail="prompt required")

    from app.llm.client import OllamaClient  # noqa: PLC0415
    from app.mcp import call_tool  # noqa: PLC0415
    from app.storage.db import get_connection  # noqa: PLC0415
    from app.storage.repository import get_kv  # noqa: PLC0415

    async with get_connection() as conn:
        endpoint = (await get_kv(conn, "byo_api_key_ollama") or "").strip()
        model = (await get_kv(conn, "ollama_model") or "").strip()
    endpoint = endpoint or "http://localhost:11434"
    model = model or "qwen2.5:7b"
    client = OllamaClient(api_key=endpoint, model=model)

    sys_prompt = (
        "Ты генерируешь файлы проекта. Верни СТРОГО JSON по схеме: массив "
        "files[], где у каждого файла path — относительный путь от корня "
        "workspace БЕЗ префикса 'workspace/' (например 'index.html', "
        "'src/app.js', 'styles/main.css'), а content — ПОЛНОЕ рабочее "
        "содержимое файла целиком, без заглушек и многоточий. Пиши реальный "
        "код. Комментарии только на русском или английском, без иероглифов."
    )
    try:
        result = await client.complete_json(
            CompletionRequest(
                system=sys_prompt, user=spec, max_tokens=4096, temperature=0.4
            ),
            _BUILD_FILES_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"генерация не удалась: {exc}"
        ) from exc

    files = result.get("files") if isinstance(result, dict) else None
    written: list[dict[str, object]] = []
    for f in files or []:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path", "")).strip()
        content = str(f.get("content", ""))
        if not path:
            continue
        res = await call_tool(
            "write_file", {"path": path, "content": content},
            user_id=session["user_id"],
        )
        written.append(
            {"path": path, "bytes": len(content.encode("utf-8")), "ok": "[ok]" in res}
        )

    if written:
        lines = "\n".join(f"- `{w['path']}` ({w['bytes']} б)" for w in written)
        note = (
            f"📦 Создал файлы ({len(written)}):\n{lines}\n\n"
            "Проверь на /workspace — это реальные файлы на диске."
        )
        await append_message(session_id, "assistant", note, model_used=f"{model} (build)")
    return JSONResponse({"ok": True, "count": len(written), "files": written})


@router.get("/api/chat/sessions/{session_id}/live", response_class=JSONResponse)
async def api_chat_live(
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """T29 — poll target for a reopened tab. Returns the in-progress
    assistant message (content grows as the model writes) so the tab shows
    generation in real time. ``{streaming, id, content}``. Multi-worker
    safe: reads the shared DB row, not per-process memory.
    """
    thread = await get_session(session["user_id"], session_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    msg = await get_streaming_message(session_id)
    if msg is None:
        return JSONResponse({"streaming": False})
    return JSONResponse(
        {"streaming": True, "id": msg["id"], "content": msg["content"]}
    )


@router.post("/api/chat/sessions/{session_id}/send", response_class=JSONResponse)
async def api_send_message(
    request: Request,
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """Append a user message, call the LLM with full session history,
    persist + return the assistant reply.

    Body: ``{"question": str}``

    The history is prepended to the system prompt as a transcript block,
    so even providers that take a single ``user`` message (Gemini's
    quirky one-shot shape, for example) see the prior exchanges.
    """
    thread = await get_session(session["user_id"], session_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="chat session not found")

    question = (
        str(body.get("question") or "").strip() if isinstance(body, dict) else ""
    )
    # T22.2 — pass-through image for vision providers (moondream, llava,
    # qwen-vl, Gemini, Claude, GPT-4o, etc). Data URL format. Big payload
    # (up to ~7 MB) — we don't store it in chat_message yet because that'd
    # bloat the DB; the UI shows it client-side only for the live turn.
    image_data_url = (
        str(body.get("image_data_url") or "") or None
        if isinstance(body, dict)
        else None
    )
    if not question and not image_data_url:
        raise HTTPException(status_code=400, detail="question or image required")
    if not question:
        question = "Опиши прикреплённую картинку."

    # Persist the user turn before we do anything else — that way a
    # subsequent crash (LLM 500 / network) doesn't lose the user's input.
    await append_message(session_id, "user", question)
    await touch_session(session["user_id"], session_id)

    # Build the transcript of prior turns. We INCLUDE the message we
    # just persisted in the history so the LLM sees the full thread,
    # then pass the latest user message as ``request.user`` for symmetry
    # with the rest of Persona's /ask code.
    history = await build_history_for_llm(session_id, max_turns=20)
    # Trim the final entry — it's the user message we'll send as
    # ``request.user`` separately.
    if history and history[-1]["role"] == "user":
        history = history[:-1]

    transcript = _bounded_transcript(history)
    active_prompt = await _base_prompt(session["user_id"], image_data_url)
    if transcript:
        system_with_history = (
            f"{active_prompt}\n\n"
            f"Предыдущие сообщения (для контекста):\n{transcript}"
        )
    else:
        system_with_history = active_prompt

    # T22 (2026-06-08) — session-pinned provider lives in kv (set by
    # update_session_model: writes llm_provider + {provider}_model +
    # byo_api_provider). make_client without args reads kv and resolves
    # the correct per-provider api_key. We DO NOT pass provider=
    # explicitly because that branch in make_client uses cfg.byo_api_key
    # (env fallback) instead of the per-provider kv slot — bug found
    # 2026-06-08: previous Yandex token leaked into OllamaClient URL.
    try:
        client = make_client(kind="chat")
    except LLMNotConfigured as exc:
        log.warning("chat.send.llm_not_configured", session_id=session_id)
        # Surface the error so the UI can render a friendly hint, and
        # also persist it as a system message so the chat log shows what
        # happened on reload.
        msg = (
            "LLM не настроен. Открой /settings/llm и выбери провайдера "
            "(YandexGPT, GigaChat, или локальный Ollama)."
        )
        await append_message(session_id, "system", msg)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    import time  # noqa: PLC0415 — keep local, avoid module top noise

    t_start = time.perf_counter()
    try:
        eff = await _get_effort(session_id)
        completion_req = CompletionRequest(
            system=system_with_history,
            user=question,
            temperature=_EFFORT_TEMP[eff],
            max_tokens=_EFFORT_TOKENS[eff],
            image_data_url=image_data_url,
        )
        answer = await client.complete(completion_req)
    except Exception as exc:
        log.warning("chat.send.llm_failed", session_id=session_id, error=str(exc))
        error_text = f"Ошибка LLM: {exc}"
        await append_message(session_id, "system", error_text)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    elapsed_ms = int((time.perf_counter() - t_start) * 1000)

    if not answer:
        answer = "(пустой ответ от модели)"

    provider_used = getattr(client, "provider", None) or getattr(
        getattr(client, "_inner", None), "provider", None
    )
    # T21: pull token counts off the inner client if the provider reported
    # them (Anthropic, OpenAI, Groq, etc all do — Ollama via OpenAI-compat
    # ALSO does). None when unavailable.
    inner = getattr(client, "_inner", client)
    in_tokens = getattr(inner, "last_input_tokens", None)
    out_tokens = getattr(inner, "last_output_tokens", None)

    assistant_msg = await append_message(
        session_id,
        "assistant",
        answer,
        model_used=provider_used,
        elapsed_ms=elapsed_ms,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
    )

    # T21: kick the summariser after every assistant turn. Cheap when
    # session is small, expensive (one LLM call) only when crossing the
    # 60-message threshold. Done async-safe via direct await — the user
    # waits a bit longer for that one message after which the summary
    # is permanent and subsequent chats are fast again.
    try:
        await maybe_summarise(session_id)
    except Exception as exc:
        log.warning("chat.summary.dispatch_failed", error=str(exc))

    return JSONResponse(
        {
            "session_id": session_id,
            "assistant": assistant_msg,
            "model_used": provider_used,
            "elapsed_ms": elapsed_ms,
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
        }
    )


@router.post("/api/chat/sessions/{session_id}/send-stream", response_model=None)
async def api_send_stream(
    request: Request,
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> StreamingResponse:
    """T22.3 (2026-06-08) — SSE-streaming version of /send.

    Why: devtunnel proxy times out single requests after 60s. Cold-start
    of a vision model can take 90+ sec on weak GPUs, so the user saw
    Error 504. With SSE the connection stays alive as tokens dribble
    in, no timeout, and the UI shows the response incrementally.

    Frame format (SSE):
        data: {"type": "delta", "text": "..."}
        data: {"type": "done", "elapsed_ms": 12345, "input_tokens": 30,
               "output_tokens": 200, "model_used": "ollama",
               "assistant_id": 42}
        data: {"type": "error", "detail": "..."}
    """
    import asyncio  # noqa: PLC0415
    import json
    import time

    thread = await get_session(session["user_id"], session_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="chat session not found")

    question = (
        str(body.get("question") or "").strip() if isinstance(body, dict) else ""
    )
    image_data_url = (
        str(body.get("image_data_url") or "") or None
        if isinstance(body, dict)
        else None
    )
    if not question and not image_data_url:
        raise HTTPException(status_code=400, detail="question or image required")
    if not question:
        question = "Опиши прикреплённую картинку."

    # Persist user turn first.
    await append_message(session_id, "user", question)
    await touch_session(session["user_id"], session_id)

    # Build history & system prompt.
    history = await build_history_for_llm(session_id, max_turns=20)
    if history and history[-1]["role"] == "user":
        history = history[:-1]
    transcript = _bounded_transcript(history)
    # T24 — per-session custom prompt + T25 — tool-use prompt fragment.
    custom_prompt = (thread.get("custom_system_prompt") or "").strip() if isinstance(thread, dict) else ""
    if custom_prompt:
        base_prompt = custom_prompt + (
            "\nНа этой странице к сообщению прикреплено изображение — "
            "рассмотри его внимательно." if image_data_url else ""
        )
    else:
        base_prompt = await _base_prompt(session["user_id"], image_data_url)

    # T25 — tools fragment: enumerate built-in tools the user enabled
    # in /admin/mcp. LLM sees them in system prompt; uses <tool>...</tool>
    # syntax to call. We parse, execute, feed back as another user msg.
    from app.mcp import (  # noqa: PLC0415
        build_tools_prompt,
        enabled_builtin_tool_names,
    )
    # T31 E3 — режим: инструменты доступны только в auto/bypass.
    _mode = await _get_mode(session_id)
    _tools_on = _mode in ("auto", "bypass")
    enabled_tools = await enabled_builtin_tool_names()
    if _tools_on:
        tools_fragment = build_tools_prompt(enabled_tools)
        base_prompt = base_prompt + tools_fragment
    base_prompt = base_prompt + _MODE_HINT.get(_mode, "")

    # T29 — installed skills: instruction sets the user pulled from GitHub
    # ("установи скилл <url>"). Inject enabled ones so the model follows them.
    try:
        from app.skills.store import enabled_skills_prompt  # noqa: PLC0415

        base_prompt = base_prompt + await enabled_skills_prompt(session["user_id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("chat.skills.inject_failed", error=str(exc))

    # T29 — auto-compaction: feed the rolling summary of older messages
    # (built by maybe_summarise) + only the last ~20 turns verbatim, instead
    # of 50 raw turns. Keeps memory while bounding input so the context
    # window never starves.
    summary_block = ""
    if isinstance(thread, dict) and thread.get("summary"):
        summary_block = (
            "\n\nСводка более ранней части беседы (помни это, "
            f"это сжатый контекст):\n{thread['summary']}"
        )
    # T29 MVP 3b — auto-memory: inject what we know about the user's recent
    # activity (hourly cards, apps/windows, voice) so the AI isn't blind.
    memory_block = ""
    try:
        from app.memory_context import build_memory_context  # noqa: PLC0415

        memory_block = await build_memory_context(question)
    except Exception as exc:  # noqa: BLE001
        log.warning("chat.memory.inject_failed", error=str(exc))
    # T29 шаг4b — pinned messages always stay in context (survive trimming).
    pinned_block = ""
    try:
        pins = await get_pinned_messages(session_id)
        if pins:
            pinned_block = (
                "\n\n── Закреплённые сообщения (пользователь выделил — помни) ──\n"
                + "\n".join(f"[{p['role']}] {p['content'][:2000]}" for p in pins)
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("chat.pin.inject_failed", error=str(exc))

    # T30 — последняя реакция пользователя на прошлый ответ → подсказка ИИ.
    reaction_block = ""
    try:
        rx = await latest_reaction(session_id)
        _rx_hint = {
            "confused": "Прошлый ответ пользователь отметил «не понял» — объясни проще, короче, с примерами.",
            "off": "Прошлый ответ отметили «не то, мимо» — смени подход; при необходимости уточни вопрос.",
            "error": "Пользователь отметил в прошлом ответе ошибку — перепроверь факты и аккуратно исправься.",
            "ok": "Прошлый ответ был полезен — держи тот же стиль и уровень детализации.",
            "fire": "Прошлый ответ зашёл — продолжай в том же духе.",
            "love": "Прошлый ответ понравился — сохраняй тон и подачу.",
        }.get(rx, "")
        if _rx_hint:
            reaction_block = "\n\n── Реакция на прошлый ответ ──\n" + _rx_hint
    except Exception as exc:  # noqa: BLE001
        log.warning("chat.reaction.inject_failed", error=str(exc))

    system_with_history = (
        f"{base_prompt}{pinned_block}{reaction_block}{memory_block}{summary_block}\n\nПоследние сообщения:\n{transcript}"
        if transcript else f"{base_prompt}{pinned_block}{reaction_block}{memory_block}{summary_block}"
    )

    async def event_stream() -> Any:
        nonlocal question

        try:
            client = make_client(kind="chat_stream")
        except LLMNotConfigured as exc:
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"
            await append_message(
                session_id,
                "system",
                "LLM не настроен. Открой /settings/llm и выбери провайдера.",
            )
            return

        # T22.10 — actually use the session-pinned model.
        chosen_model = thread.get("model")

        # T24 — auto-switch to vision-capable model when image attached.
        # Heuristic: if user attached image and current model name doesn't
        # contain a vision marker (vl/vision/llava/moondream/vlava), look
        # in the same provider's installed models for one that does and
        # use it for THIS turn only. User's saved model preference stays
        # untouched.
        if image_data_url and chosen_model:
            if not any(kw in chosen_model.lower() for kw in (
                "vl", "vision", "llava", "moondream",
            )):
                vision_model = await _find_vision_model_for_provider(thread.get("provider"))
                if vision_model:
                    log.info(
                        "chat.stream.auto_vision_swap",
                        from_model=chosen_model,
                        to_model=vision_model,
                    )
                    chosen_model = vision_model

        if chosen_model:
            inner_obj = getattr(client, "_inner", client)
            if hasattr(inner_obj, "_model"):
                inner_obj._model = chosen_model
                log.info(
                    "chat.stream.session_model_pin",
                    session_id=session_id,
                    pinned_model=chosen_model,
                )

        t_start = time.perf_counter()
        chunks: list[str] = []
        # T29 — incremental persistence: the assistant row is created on the
        # first delta and its content is flushed to the DB ~every second, so
        # a reopened tab can poll /live and watch the answer grow in real time.
        streaming_msg_id: int | None = None
        last_save = 0.0
        eff = await _get_effort(session_id)
        completion_req = CompletionRequest(
            system=system_with_history,
            user=question,
            temperature=_EFFORT_TEMP[eff],
            max_tokens=_EFFORT_TOKENS[eff],
            image_data_url=image_data_url,
        )

        # First yield primes the SSE connection so the browser/proxy sees
        # bytes flowing within the first second — no 504, even if the
        # model takes 90 sec to produce its first token.
        yield "data: {\"type\":\"meta\",\"started\":true}\n\n"

        # T22.5 (2026-06-08) — devtunnel proxies BUFFER SSE responses
        # unless data flows constantly. Cold-start vision models take
        # 90+ sec to produce the first token; during that window
        # devtunnel sees no data and dies. Use asyncio.Queue + a
        # background producer so we can emit keepalive pings every 1
        # sec while waiting for the stream to start.
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=512)

        async def producer() -> None:
            try:
                async for delta in client.stream(completion_req):
                    if not delta:
                        continue
                    await queue.put(("delta", delta))
            except Exception as exc:
                # T22.10 — surface friendlier message for the common
                # 'model does not support multimodal' case.
                msg = str(exc)
                if image_data_url and (
                    "multimodal" in msg.lower()
                    or "does not support" in msg.lower()
                ):
                    msg = (
                        "Эта модель не понимает картинки. Тапни имя "
                        "модели внизу и выбери vision-модель — "
                        "qwen2.5vl:7b, qwen2.5vl:3b или moondream."
                    )
                await queue.put(("error", msg))
            finally:
                await queue.put(("eof", ""))

        prod_task = asyncio.create_task(producer())
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    # T22.8 — devtunnel ignored ':comment\\n\\n' (SSE-spec
                    # comments) so it still timeouted at 60s on cold-start.
                    # Use a real `data:` event with type=keepalive — that's
                    # actual bytes devtunnel logs as response progress.
                    # Client-side JS ignores 'keepalive' events.
                    yield "data: {\"type\":\"keepalive\"}\n\n"
                    continue
                if kind == "eof":
                    break
                if kind == "delta":
                    chunks.append(payload)
                    # Create the row on first content; flush ~every 1s after.
                    if streaming_msg_id is None:
                        streaming_msg_id = await start_streaming_message(
                            session_id, "assistant", model_used=None
                        )
                    now = time.perf_counter()
                    if now - last_save >= 1.0:
                        last_save = now
                        try:
                            await update_streaming_message(
                                streaming_msg_id, "".join(chunks)
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.debug("chat.stream.persist_failed", error=str(exc))
                    yield f"data: {json.dumps({'type': 'delta', 'text': payload})}\n\n"
                elif kind == "error":
                    log.warning("chat.stream.failed", error=payload)
                    err = f"Ошибка LLM: {payload}"
                    yield f"data: {json.dumps({'type': 'error', 'detail': payload})}\n\n"
                    await append_message(session_id, "system", err)
                    return
        except asyncio.CancelledError:
            log.info("chat.stream.cancelled", session_id=session_id)
            prod_task.cancel()
            raise
        except Exception as exc:
            log.warning("chat.stream.failed", error=str(exc))
            err = f"Ошибка LLM: {exc}"
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"
            await append_message(session_id, "system", err)
            prod_task.cancel()
            return
        finally:
            # Producer naturally ends on EOF but if we exit early (CancelledError
            # or send error) we need to cancel it explicitly to avoid the task
            # leak warning during uvicorn shutdown.
            if not prod_task.done():
                prod_task.cancel()

        full = "".join(chunks).strip() or "(пустой ответ от модели)"
        provider_used = getattr(client, "provider", None) or getattr(
            getattr(client, "_inner", None), "provider", None
        )
        inner = getattr(client, "_inner", client)

        # T25 — tool-use loop. If LLM emitted <tool>...</tool> calls,
        # parse + execute (max 5 round-trips to avoid infinite loops),
        # then continue the conversation with results in context.
        from app.mcp import call_tool, parse_tool_calls  # noqa: PLC0415

        # T29 — track already-executed calls by their exact <tool>…</tool>
        # text. `full` accumulates every round, so without this the original
        # call is re-parsed and re-run each round (the "выполнил 3 раза" bug).
        executed_raws: set[str] = set()
        # T31 E3 — в режимах plan/ask инструменты НЕ выполняются (только план/спрос).
        for _round in (range(5) if _tools_on else range(0)):
            tool_calls = [
                tc for tc in parse_tool_calls(full)
                if tc.get("raw") not in executed_raws
            ]
            if not tool_calls:
                break
            for tc in tool_calls:
                executed_raws.add(tc.get("raw", ""))
            # Execute each tool call serially, stream visible markers.
            tool_results: list[str] = []
            for tc in tool_calls:
                yield (
                    f"data: {json.dumps({'type': 'delta', 'text': chr(10) + chr(10) + '🔧 ' + tc['name'] + '...' + chr(10)})}\n\n"
                )
                result = await call_tool(tc["name"], tc["args"], user_id=session["user_id"])
                tool_results.append(
                    f"<tool_result name=\"{tc['name']}\">\n{result}\n</tool_result>"
                )
                chunks.append(f"\n\n🔧 {tc['name']}({json.dumps(tc['args'], ensure_ascii=False)})\n")
                chunks.append(f"\n```\n{result}\n```\n")
                yield (
                    f"data: {json.dumps({'type': 'delta', 'text': chr(10) + '```' + chr(10) + result + chr(10) + '```' + chr(10)})}\n\n"
                )

            # Continue conversation: ask model to respond after tool results
            follow_up = (
                "Результаты вызовов инструментов:\n" + "\n".join(tool_results)
                + "\n\nПродолжи: дай финальный ответ пользователю на основе этих результатов."
            )
            try:
                follow_req = CompletionRequest(
                    system=base_prompt,
                    user=follow_up,
                    temperature=0.7,
                    max_tokens=4096,
                )
                next_chunks: list[str] = []
                async for delta in client.stream(follow_req):
                    if not delta:
                        continue
                    next_chunks.append(delta)
                    yield f"data: {json.dumps({'type': 'delta', 'text': delta})}\n\n"
                next_full = "".join(next_chunks).strip()
                if next_full:
                    full = full + "\n\n" + next_full
                    chunks.append("\n\n" + next_full)
                else:
                    break  # empty follow-up → done
            except Exception as exc:
                log.warning("chat.tool_followup.failed", error=str(exc))
                break

        elapsed_ms = int((time.perf_counter() - t_start) * 1000)
        in_tokens = getattr(inner, "last_input_tokens", None)
        out_tokens = getattr(inner, "last_output_tokens", None)

        # T29 — finalize the streaming row (create one if no delta ever
        # arrived: tool-only or empty response).
        if streaming_msg_id is None:
            streaming_msg_id = await start_streaming_message(
                session_id, "assistant", model_used=provider_used
            )
        await finalize_streaming_message(
            streaming_msg_id, full,
            model_used=provider_used,
            elapsed_ms=elapsed_ms,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )
        assistant_msg_id = streaming_msg_id

        # T23 — record Q&A pair for future PersonaAI fine-tune. Kept INLINE
        # (it's fast DB writes) so the row exists before the user can rate.
        try:
            from app.training import record_qa_pair  # noqa: PLC0415
            # Need the user_message_id we appended at the top of this
            # route. Re-fetch the latest user message in this session as
            # a tactical workaround — chat_message ids are monotonic so
            # MAX(id) is reliable here.
            from app.storage.db import get_connection  # noqa: PLC0415
            async with get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT id FROM chat_message "
                    "WHERE session_id = ? AND role = 'user' "
                    "ORDER BY id DESC LIMIT 1",
                    (session_id,),
                )
                user_row = await cursor.fetchone()
            user_msg_id = int(user_row["id"]) if user_row else 0
            await record_qa_pair(
                session_id=session_id,
                user_message_id=user_msg_id,
                asst_message_id=assistant_msg_id,
                user_text=question,
                assistant_text=full,
                system_prompt=base_prompt,
                context_turns=history,
                image_present=bool(image_data_url),
                provider=provider_used,
                model=getattr(inner, "_model", None),
            )
        except Exception as exc:
            log.warning("chat.training.record_failed", error=str(exc))

        # T29 — send 'done' NOW so the composer unlocks the instant the
        # answer is complete. The auto-summary is a SLOW separate LLM call;
        # running it inline held the SSE stream open and froze the input for
        # seconds ("много времени впустую"). Fire it in the background.
        done = {
            "type": "done",
            "elapsed_ms": elapsed_ms,
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "model_used": provider_used,
            "assistant_id": assistant_msg_id,
        }
        yield f"data: {json.dumps(done)}\n\n"

        async def _bg_summarise(sid: int) -> None:
            try:
                await maybe_summarise(sid)
            except Exception as exc:  # noqa: BLE001
                log.warning("chat.summary.dispatch_failed", error=str(exc))

        import asyncio as _asyncio  # noqa: PLC0415
        _asyncio.create_task(_bg_summarise(session_id))

    # T29 — run the generation in a DETACHED task so it survives the user
    # closing the page. `event_stream()` (unchanged) is driven by `_pump`,
    # which keeps iterating it to the end — incl. tool calls + persisting
    # the assistant message — even when no client is attached. The HTTP
    # response just relays frames; on disconnect only the relay stops.
    gen = _LiveGen()
    _LIVE_GENS[session_id] = gen

    async def _pump() -> None:
        try:
            async for frame in event_stream():
                gen.emit(frame)
        except Exception as exc:  # noqa: BLE001
            log.warning("chat.gen.pump_failed", session_id=session_id, error=str(exc))
        finally:
            gen.finish()
            if _LIVE_GENS.get(session_id) is gen:
                _LIVE_GENS.pop(session_id, None)

    gen.task = asyncio.create_task(_pump())

    async def _relay() -> Any:
        q = gen.subscribe()
        try:
            while True:
                frame = await q.get()
                if frame is None:
                    break
                yield frame
        finally:
            gen.unsubscribe(q)

    return StreamingResponse(
        _relay(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # tells nginx/proxy not to buffer
        },
    )


@router.post("/api/chat/sessions/{session_id}/model", response_class=JSONResponse)
async def api_set_model(
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """Pin a provider+model to one chat session — that's the per-session
    model picker shown in the chat header.

    Body: ``{"provider": "ollama", "model": "gemma3:4b"}``

    Also writes the corresponding ``{provider}_model`` kv row so the
    client constructor for that provider picks up the override on the
    very next call from this session.
    """
    provider = str(body.get("provider") or "").strip().lower()
    model = str(body.get("model") or "").strip()
    if not provider:
        raise HTTPException(status_code=400, detail="provider required")
    ok = await update_session_model(
        session["user_id"], session_id, provider, model or None
    )
    if not ok:
        raise HTTPException(status_code=404)
    # Stash the model under the kv key that make_client reads on
    # construction, so the next /send call picks it up.
    if model:
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import set_kv  # noqa: PLC0415
        async with get_connection() as conn:
            await set_kv(conn, f"{provider}_model", model)
            # Also persist the active provider globally so /ask + others
            # use it too. User expectation: "I switched in chat, now I
            # want this everywhere."
            await set_kv(conn, "llm_provider", provider)
            await set_kv(conn, "byo_api_provider", provider)
    return JSONResponse({"ok": True, "provider": provider, "model": model or None})


@router.post("/api/chat/compare", response_class=JSONResponse)
async def api_compare_models(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """T24 — run the same question through N models in parallel.
    Returns ``[{provider, model, answer, elapsed_ms, error?}, ...]``.

    Body: ``{"question": str, "image_data_url": str|null,
             "models": [{provider, model}, ...]}``
    """
    import asyncio  # noqa: PLC0415
    import time  # noqa: PLC0415
    from app.llm.client import (  # noqa: PLC0415
        CompletionRequest,
        LLMNotConfigured,
        make_client,
    )

    question = str(body.get("question") or "").strip()
    image_data_url = str(body.get("image_data_url") or "") or None
    models = body.get("models") or []
    if not question and not image_data_url:
        raise HTTPException(status_code=400, detail="question or image required")
    if not isinstance(models, list) or not models:
        raise HTTPException(status_code=400, detail="models list required")
    if len(models) > 4:
        raise HTTPException(status_code=400, detail="max 4 models in compare")

    # T29 — was hard-coded _SYSTEM_PROMPT_RU, which IGNORED the user's
    # custom system prompt → compare results disagreed with chat. Use the
    # active prompt like /send and /send-stream.
    base_prompt = await _base_prompt(session["user_id"], image_data_url)

    async def one(provider: str, model: str) -> dict[str, Any]:
        try:
            client = make_client(kind="chat_compare")
        except LLMNotConfigured as exc:
            return {"provider": provider, "model": model, "answer": "", "error": str(exc)}
        inner_obj = getattr(client, "_inner", client)
        if hasattr(inner_obj, "_model"):
            inner_obj._model = model
        req = CompletionRequest(
            system=base_prompt,
            user=question or "Опиши прикреплённую картинку.",
            temperature=0.7,
            max_tokens=512,
            image_data_url=image_data_url,
        )
        t0 = time.perf_counter()
        try:
            ans = await client.complete(req)
            return {
                "provider": provider,
                "model": model,
                "answer": ans,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            }
        except Exception as exc:
            return {
                "provider": provider,
                "model": model,
                "answer": "",
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "error": str(exc)[:300],
            }

    tasks = [
        one(str(m.get("provider", "")), str(m.get("model", "")))
        for m in models if isinstance(m, dict)
    ]
    results = await asyncio.gather(*tasks)
    return JSONResponse({"results": results})


@router.post("/api/chat/sessions/{session_id}/system-prompt", response_class=JSONResponse)
async def api_set_system_prompt(
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """T24 — set the per-session 'role' / custom system prompt. Empty
    string clears it (back to default)."""
    text = str(body.get("prompt") or "").strip()
    thread = await get_session(session["user_id"], session_id)
    if thread is None:
        raise HTTPException(status_code=404)
    from app.storage.db import get_connection  # noqa: PLC0415
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE chat_session SET custom_system_prompt = ?, "
            "                        updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?",
            (text or None, session_id, session["user_id"]),
        )
        await conn.commit()
    return JSONResponse({"ok": True, "prompt": text or None})


@router.post("/api/chat/messages/{message_id}/rate", response_class=JSONResponse)
async def api_rate_message(
    message_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """T24 — set 👍/👎 on an assistant message. Lookups the
    training_dataset row that points at this message_id and stamps
    rating there too, so the dataset reflects user judgment."""
    rating = int(body.get("rating") or 0)
    if rating not in (-1, 0, 1):
        raise HTTPException(status_code=400, detail="rating must be -1, 0, or 1")
    from app.storage.db import get_connection  # noqa: PLC0415
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE training_dataset SET rating = ? WHERE asst_message_id = ?",
            (rating, message_id),
        )
        await conn.commit()
        affected = cursor.rowcount
    return JSONResponse({"ok": True, "rating": rating, "rows_updated": affected})


@router.post("/api/chat/messages/{message_id}/rate-span", response_class=JSONResponse)
async def api_rate_span(
    message_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """T29 — like/dislike a SELECTED fragment of an answer, with the text,
    so the dataset captures exactly what bothered the user."""
    rating = int(body.get("rating") or 0)
    selected = str(body.get("selected_text") or "").strip()
    session_id = int(body.get("session_id") or 0)
    if rating not in (-1, 1) or not selected:
        raise HTTPException(status_code=400, detail="selected_text + rating(-1|1) required")
    await add_span_rating(message_id, session_id, selected, rating)
    return JSONResponse({"ok": True})


@router.post("/api/chat/messages/{message_id}/pin", response_class=JSONResponse)
async def api_pin_message(
    message_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """T29 — pin/unpin a message so it stays in context after trimming."""
    pinned = bool(body.get("pinned"))
    await set_message_pinned(message_id, pinned)
    return JSONResponse({"ok": True, "pinned": pinned})


_ALLOWED_REACTIONS = {"confused", "ok", "fire", "love", "off", "error"}


@router.post("/api/chat/messages/{message_id}/react", response_class=JSONResponse)
async def api_react_message(
    message_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """T30 — поставить/снять реакцию на ответ ИИ. ИИ учтёт её в контексте."""
    reaction = str(body.get("reaction") or "").strip()
    if reaction and reaction not in _ALLOWED_REACTIONS:
        return JSONResponse({"ok": False, "error": "unknown reaction"}, status_code=400)
    await set_reaction(message_id, session["user_id"], reaction)
    return JSONResponse({"ok": True, "reaction": reaction})


@router.post("/api/chat/sessions/{session_id}/effort", response_class=JSONResponse)
async def api_set_effort(
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """T31 E2 — выбрать «эффорт» (мощность) для сессии: fast/normal/deep."""
    eff = str(body.get("effort") or "normal").strip()
    if eff not in _EFFORT_TOKENS:
        eff = "normal"
    await _set_effort(session_id, eff)
    return JSONResponse({"ok": True, "effort": eff})


@router.post("/api/chat/sessions/{session_id}/mode", response_class=JSONResponse)
async def api_set_mode(
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """T31 E3 — выбрать режим работы сессии: plan/ask/auto/bypass."""
    mode = str(body.get("mode") or "auto").strip()
    if mode not in _MODES:
        mode = "auto"
    await _set_mode(session_id, mode)
    return JSONResponse({"ok": True, "mode": mode})


@router.post("/api/chat/sessions/{session_id}/rename", response_class=JSONResponse)
async def api_rename_session(
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    ok = await rename_session(session["user_id"], session_id, title)
    if not ok:
        raise HTTPException(status_code=404)
    return JSONResponse({"ok": True})


@router.delete("/api/chat/sessions/{session_id}", response_class=JSONResponse)
async def api_delete_session(
    session_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    ok = await delete_session(session["user_id"], session_id)
    if not ok:
        raise HTTPException(status_code=404)
    return JSONResponse({"ok": True})
