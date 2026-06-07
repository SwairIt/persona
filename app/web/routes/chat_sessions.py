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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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
)
from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["chat"])
log = get_logger("persona.chat.routes")

_SYSTEM_PROMPT_RU = (
    "Ты — память пользователя. Отвечай естественно, по-русски (если "
    "пользователь не пишет на другом языке). Используй контекст из "
    "предыдущих сообщений беседы. Не выдумывай факты — если не знаешь, "
    "честно скажи."
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
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")

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
