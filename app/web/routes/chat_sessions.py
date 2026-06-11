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

_SYSTEM_PROMPT_RU = (
    # Identity — friendly but not sycophantic
    "Ты — личный AI пользователя. Общайся как умный, прямой друг: "
    "тепло, по-человечески, с эмоциями где они уместны (можешь "
    "пошутить, проявить интерес, согласиться или возразить). Не как "
    "робот-секретарша с шаблонными формулами «Конечно! С радостью!». "
    "Помогай со всем: код, дизайн, идеи, картинки, разговоры, перевод, "
    "анализ. Никогда не отказывайся со словами «я просто ассистент» "
    "или «не могу создавать код» — ты можешь.\n\n"
    # Style
    "Стиль:\n"
    "- Сразу к делу. Без преамбул «Это интересный вопрос».\n"
    "- Markdown для форматирования: ```язык``` для кода с указанием "
    "языка, **жирный** для акцентов, ## заголовки только для длинных "
    "ответов на сложные вопросы.\n"
    "- Если не уверен — честно скажи «не уверен» или «не знаю». Не "
    "выдумывай факты.\n"
    "- ЯЗЫК: отвечай ТОЛЬКО на русском или английском — на том, на "
    "котором написал пользователь. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ китайские "
    "иероглифы и любые CJK-символы — ни одного знака, никогда, даже в "
    "примерах или комментариях кода. Один язык на весь ответ, без "
    "смешивания.\n"
    "- Длина ответа = масштабу вопроса. На «привет» — пара фраз. На "
    "сложный код — столько сколько нужно.\n\n"
    # Plan only when truly needed
    "Планы и чек-листы:\n"
    "ИСПОЛЬЗУЙ ПЛАН ТОЛЬКО ДЛЯ СЛОЖНЫХ ЗАДАЧ из >3 явных шагов "
    "(большая программа, рефакторинг, дизайн системы, многоэтапный "
    "анализ). На простые вопросы — отвечай одним сообщением БЕЗ плана.\n\n"
    "Когда план реально нужен — формат markdown с переносами строк:\n\n"
    "**План:**\n\n"
    "- [ ] первый шаг\n"
    "- [ ] второй шаг\n"
    "- [ ] третий шаг\n\n"
    "После выполнения переписываешь список с галочками:\n\n"
    "**Готово:**\n\n"
    "- [x] первый шаг — что сделал\n"
    "- [x] второй шаг — что сделал\n\n"
    "Каждый пункт на ОТДЕЛЬНОЙ СТРОКЕ. Пустая строка перед списком."
)

_SYSTEM_PROMPT_VISION = (
    "Ты — личный AI с компьютерным зрением. К сообщению прикреплено "
    "изображение — рассмотри и опиши что видишь. Будь точным; если "
    "что-то нечитаемо — скажи. Не отказывайся от описания. Не ври. "
    "Отвечай по-русски."
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
            max_tokens=4096,
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
    # T24 — per-session custom prompt + T25 — tool-use prompt fragment.
    custom_prompt = (thread.get("custom_system_prompt") or "").strip() if isinstance(thread, dict) else ""
    if custom_prompt:
        base_prompt = custom_prompt + (
            "\nНа этой странице к сообщению прикреплено изображение — "
            "рассмотри его внимательно." if image_data_url else ""
        )
    else:
        base_prompt = _SYSTEM_PROMPT_VISION if image_data_url else _SYSTEM_PROMPT_RU

    # T25 — tools fragment: enumerate built-in tools the user enabled
    # in /admin/mcp. LLM sees them in system prompt; uses <tool>...</tool>
    # syntax to call. We parse, execute, feed back as another user msg.
    from app.mcp import (  # noqa: PLC0415
        build_tools_prompt,
        enabled_builtin_tool_names,
    )
    enabled_tools = await enabled_builtin_tool_names()
    tools_fragment = build_tools_prompt(enabled_tools)
    base_prompt = base_prompt + tools_fragment

    # T29 — installed skills: instruction sets the user pulled from GitHub
    # ("установи скилл <url>"). Inject enabled ones so the model follows them.
    try:
        from app.skills.store import enabled_skills_prompt  # noqa: PLC0415

        base_prompt = base_prompt + await enabled_skills_prompt(session["user_id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("chat.skills.inject_failed", error=str(exc))

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
        completion_req = CompletionRequest(
            system=system_with_history,
            user=question,
            temperature=0.7,
            max_tokens=4096,
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
        for _round in range(5):
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

        assistant_msg = await append_message(
            session_id, "assistant", full,
            model_used=provider_used,
            elapsed_ms=elapsed_ms,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )

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
                asst_message_id=assistant_msg["id"],
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
            "assistant_id": assistant_msg["id"],
        }
        yield f"data: {json.dumps(done)}\n\n"

        async def _bg_summarise(sid: int) -> None:
            try:
                await maybe_summarise(sid)
            except Exception as exc:  # noqa: BLE001
                log.warning("chat.summary.dispatch_failed", error=str(exc))

        import asyncio as _asyncio  # noqa: PLC0415
        _asyncio.create_task(_bg_summarise(session_id))

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

    base_prompt = _SYSTEM_PROMPT_VISION if image_data_url else _SYSTEM_PROMPT_RU

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
