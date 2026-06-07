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

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.chat import (
    append_message,
    build_history_for_llm,
    create_session,
    delete_session,
    get_session,
    list_messages,
    list_sessions,
    maybe_summarise,
    rename_session,
    touch_session,
    update_session_model,
)
from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["chat"])
log = get_logger("persona.chat.routes")

_SYSTEM_PROMPT_RU = (
    "Ты — Persona, личный AI-помощник пользователя. "
    "Главное правило: будь точным. Если знаешь — отвечай уверенно и "
    "конкретно. Если не уверен — честно скажи: «не знаю» или «не уверен», "
    "но не выдумывай факты, имена, даты, цитаты. "
    "Других ограничений у тебя нет: помогай со всем что попросят — "
    "анализом, советами, кодом, идеями, описанием картинок, переводом, "
    "обсуждением жизни. Используй контекст беседы. Отвечай по-русски "
    "(если пользователь не пишет на другом языке) — естественно, без "
    "лишних оговорок и дисклеймеров."
)

# T22.7 (2026-06-08) — когда есть картинка, явный prompt про vision
# чтобы модель не отказывалась под предлогом 'я не выдумываю'.
_SYSTEM_PROMPT_VISION = (
    "Ты — Persona с компьютерным зрением. К сообщению пользователя "
    "прикреплено изображение — рассмотри его и опиши что видишь. "
    "Будь точным: если что-то нечитаемо или непонятно — скажи. "
    "Не отказывайся от описания: это твоя основная функция. "
    "Других ограничений у тебя нет. Отвечай по-русски (если "
    "пользователь не пишет на другом языке) — естественно, без "
    "лишних дисклеймеров."
)


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
    return templates.TemplateResponse(
        request,
        "chat_index.html",
        {
            "title": thread["title"],
            "active_nav": "ask",
            "sessions": sessions,
            "active_session": thread,
            "messages": messages,
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

    transcript = "\n".join(
        f"[{turn['role']}] {turn['content']}" for turn in history
    )
    if transcript:
        system_with_history = (
            f"{_SYSTEM_PROMPT_RU}\n\n"
            f"Предыдущие сообщения (для контекста):\n{transcript}"
        )
    else:
        system_with_history = _SYSTEM_PROMPT_RU

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
        completion_req = CompletionRequest(
            system=system_with_history,
            user=question,
            temperature=0.7,
            max_tokens=1024,
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
    history = await build_history_for_llm(session_id, max_turns=50)
    if history and history[-1]["role"] == "user":
        history = history[:-1]
    transcript = "\n".join(
        f"[{turn['role']}] {turn['content']}" for turn in history
    )
    # T22.7 — vision-friendly prompt when image attached.
    base_prompt = _SYSTEM_PROMPT_VISION if image_data_url else _SYSTEM_PROMPT_RU
    system_with_history = (
        f"{base_prompt}\n\nПредыдущие сообщения (для контекста):\n{transcript}"
        if transcript else base_prompt
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

        t_start = time.perf_counter()
        chunks: list[str] = []
        completion_req = CompletionRequest(
            system=system_with_history,
            user=question,
            temperature=0.7,
            max_tokens=1024,
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
                await queue.put(("error", str(exc)))
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
        elapsed_ms = int((time.perf_counter() - t_start) * 1000)
        provider_used = getattr(client, "provider", None) or getattr(
            getattr(client, "_inner", None), "provider", None
        )
        inner = getattr(client, "_inner", client)
        in_tokens = getattr(inner, "last_input_tokens", None)
        out_tokens = getattr(inner, "last_output_tokens", None)

        assistant_msg = await append_message(
            session_id, "assistant", full,
            model_used=provider_used,
            elapsed_ms=elapsed_ms,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )

        try:
            await maybe_summarise(session_id)
        except Exception as exc:
            log.warning("chat.summary.dispatch_failed", error=str(exc))

        done = {
            "type": "done",
            "elapsed_ms": elapsed_ms,
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "model_used": provider_used,
            "assistant_id": assistant_msg["id"],
        }
        yield f"data: {json.dumps(done)}\n\n"

    return StreamingResponse(
        event_stream(),
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
