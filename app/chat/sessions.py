"""CRUD + LLM-history shaping for the persistent chat threads."""

from __future__ import annotations

from typing import Any, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.chat")

# Cap on stored content per message. Anything bigger is almost certainly
# a paste from a code review or a runaway model output — refuse to write
# rather than bloat the table.
_MAX_CONTENT_BYTES = 32 * 1024

# T20 (2026-06-07): bumped from 20 → 50 because the user said
# "you promised to save EVERYTHING". The DB ALWAYS stored every message
# (chat_message is append-only). The 20-cap was only on what gets
# replayed to the model per turn. 50 turns ≈ ~10k tokens of history
# which still fits comfortably in Qwen 3B's 32k window after the system
# prompt and user input. For sessions older than 50 turns the route
# layer can later inject a summary of the omitted prefix — for now we
# just show recent context, which covers 99% of practical chats.
_DEFAULT_HISTORY_TURNS = 50


class ChatSession(TypedDict):
    id: int
    user_id: int
    title: str
    provider: str | None
    model: str | None
    created_at: str
    updated_at: str
    summary: str | None
    summary_up_to_id: int


class ChatMessage(TypedDict):
    id: int
    session_id: int
    role: str
    content: str
    model_used: str | None
    created_at: str
    elapsed_ms: int | None
    input_tokens: int | None
    output_tokens: int | None


def _row_to_session(row: Any) -> ChatSession:
    # T21 — older sessions (pre-migration-161) won't have summary cols
    # populated. ``row["x"] if "x" in row.keys() else None`` is the
    # defensive read; aiosqlite.Row supports ``in`` but raises KeyError
    # on missing cols, so we use ``.keys()`` membership.
    keys = set(row.keys())
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "title": str(row["title"]),
        "provider": str(row["provider"]) if row["provider"] is not None else None,
        "model": str(row["model"]) if row["model"] is not None else None,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "summary": (
            str(row["summary"])
            if "summary" in keys and row["summary"] is not None
            else None
        ),
        "summary_up_to_id": (
            int(row["summary_up_to_id"])
            if "summary_up_to_id" in keys and row["summary_up_to_id"] is not None
            else 0
        ),
    }


def _row_to_message(row: Any) -> ChatMessage:
    keys = set(row.keys())
    return {
        "id": int(row["id"]),
        "session_id": int(row["session_id"]),
        "role": str(row["role"]),
        "content": str(row["content"]),
        "model_used": (
            str(row["model_used"]) if row["model_used"] is not None else None
        ),
        "created_at": str(row["created_at"]),
        "elapsed_ms": (
            int(row["elapsed_ms"])
            if "elapsed_ms" in keys and row["elapsed_ms"] is not None
            else None
        ),
        "input_tokens": (
            int(row["input_tokens"])
            if "input_tokens" in keys and row["input_tokens"] is not None
            else None
        ),
        "output_tokens": (
            int(row["output_tokens"])
            if "output_tokens" in keys and row["output_tokens"] is not None
            else None
        ),
        "is_streaming": (
            bool(row["is_streaming"])
            if "is_streaming" in keys and row["is_streaming"] is not None
            else False
        ),
        # T29 — rating lives in training_dataset; brought in via LEFT JOIN so
        # the chat UI restores 👍/👎 state on reload.
        "rating": (
            int(row["rating"])
            if "rating" in keys and row["rating"] is not None
            else 0
        ),
        "is_pinned": (
            bool(row["is_pinned"])
            if "is_pinned" in keys and row["is_pinned"] is not None
            else False
        ),
        # T30 — реакция пользователя (🤔/✅/🔥/❤️/😕/⚠️), brought in via subquery
        "reaction": (
            str(row["reaction"])
            if "reaction" in keys and row["reaction"] is not None
            else ""
        ),
    }


def _derive_title(first_user_message: str) -> str:
    """Pick a short title from the user's first message.

    Strip whitespace, cut to 60 chars, no newlines. We keep this purely
    deterministic — calling an LLM just to summarise the title would be
    fancy but wastes a real query on a UI nicety.
    """
    cleaned = " ".join((first_user_message or "").split()).strip()
    if not cleaned:
        return "Без названия"
    return cleaned[:60]


async def create_session(
    user_id: int,
    title: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> ChatSession:
    """Open a new chat thread. Title can come later via ``rename_session``."""
    chosen_title = (title or "").strip() or "Без названия"
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO chat_session (user_id, title, provider, model) "
            "VALUES (?, ?, ?, ?)",
            (user_id, chosen_title[:120], provider, model),
        )
        await conn.commit()
        session_id = cursor.lastrowid or 0
        cursor = await conn.execute(
            "SELECT * FROM chat_session WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("chat_session insert reported success but lookup failed")
    log.info("chat.session.created", session_id=session_id, user_id=user_id)
    return _row_to_session(row)


async def list_sessions(user_id: int, limit: int = 50) -> list[ChatSession]:
    """Recent-first sidebar listing."""
    safe_limit = max(1, min(500, int(limit)))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM chat_session "
            "WHERE user_id = ? "
            "ORDER BY updated_at DESC "
            "LIMIT ?",
            (user_id, safe_limit),
        )
        rows = await cursor.fetchall()
    return [_row_to_session(r) for r in rows]


async def get_session(user_id: int, session_id: int) -> ChatSession | None:
    """Return one session scoped to its owner. ``None`` for not-found / wrong-user."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM chat_session WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )
        row = await cursor.fetchone()
    return _row_to_session(row) if row is not None else None


async def list_messages(session_id: int, limit: int = 500) -> list[ChatMessage]:
    """Chronological history of one session. Use the helper-shaped
    :func:`build_history_for_llm` when piping into the model."""
    safe_limit = max(1, min(2000, int(limit)))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT m.*, "
            "  (SELECT td.rating FROM training_dataset td "
            "   WHERE td.asst_message_id = m.id ORDER BY td.id DESC LIMIT 1) "
            "  AS rating, "
            "  (SELECT cr.reaction FROM chat_reaction cr "
            "   WHERE cr.message_id = m.id ORDER BY cr.id DESC LIMIT 1) "
            "  AS reaction "
            "FROM chat_message m "
            "WHERE m.session_id = ? "
            "ORDER BY m.id ASC "
            "LIMIT ?",
            (session_id, safe_limit),
        )
        rows = await cursor.fetchall()
    return [_row_to_message(r) for r in rows]


async def append_message(
    session_id: int,
    role: str,
    content: str,
    model_used: str | None = None,
    *,
    elapsed_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> ChatMessage:
    """Log one turn. Updates ``chat_session.updated_at`` so the sidebar
    keeps the thread on top.

    T21: optional ``elapsed_ms``/``input_tokens``/``output_tokens`` for
    assistant messages — recorded so the UI can render 'ответ за 12.3s'
    and a context-usage bar.
    """
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"unknown role: {role!r}")
    text = (content or "").strip()
    if not text:
        raise ValueError("content must not be empty")
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_CONTENT_BYTES:
        text = encoded[:_MAX_CONTENT_BYTES].decode("utf-8", errors="ignore")
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO chat_message "
            "  (session_id, role, content, model_used, "
            "   elapsed_ms, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, role, text, model_used,
                elapsed_ms, input_tokens, output_tokens,
            ),
        )
        await conn.commit()
        new_id = cursor.lastrowid or 0
        await conn.execute(
            "UPDATE chat_session SET updated_at = datetime('now') WHERE id = ?",
            (session_id,),
        )
        await conn.commit()
        cursor = await conn.execute(
            "SELECT * FROM chat_message WHERE id = ?", (new_id,)
        )
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("chat_message insert reported success but lookup failed")
    return _row_to_message(row)


async def start_streaming_message(
    session_id: int, role: str = "assistant", model_used: str | None = None
) -> int:
    """T29 — create an in-progress (is_streaming=1) message row and return
    its id. Content starts empty and grows via update_streaming_message;
    finalize_streaming_message flips is_streaming off at the end. A
    reopened tab reads this row live."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO chat_message "
            "  (session_id, role, content, model_used, is_streaming) "
            "VALUES (?, ?, '', ?, 1)",
            (session_id, role, model_used),
        )
        await conn.commit()
        new_id = cursor.lastrowid or 0
        await conn.execute(
            "UPDATE chat_session SET updated_at = datetime('now') WHERE id = ?",
            (session_id,),
        )
        await conn.commit()
    return int(new_id)


async def update_streaming_message(message_id: int, content: str) -> None:
    """T29 — update the growing content of an in-progress message (throttled
    by the caller). Keeps is_streaming=1."""
    text = content or ""
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_CONTENT_BYTES:
        text = encoded[:_MAX_CONTENT_BYTES].decode("utf-8", errors="ignore")
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE chat_message SET content = ? WHERE id = ?",
            (text, message_id),
        )
        await conn.commit()


async def finalize_streaming_message(
    message_id: int,
    content: str,
    *,
    model_used: str | None = None,
    elapsed_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """T29 — write final content + metadata and flip is_streaming off."""
    text = (content or "").strip() or "(пустой ответ)"
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_CONTENT_BYTES:
        text = encoded[:_MAX_CONTENT_BYTES].decode("utf-8", errors="ignore")
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE chat_message SET content = ?, model_used = ?, "
            "elapsed_ms = ?, input_tokens = ?, output_tokens = ?, "
            "is_streaming = 0 WHERE id = ?",
            (text, model_used, elapsed_ms, input_tokens, output_tokens, message_id),
        )
        await conn.commit()


def _snippet(content: str, query: str, width: int = 140) -> str:
    """A short excerpt centred on the first case-insensitive match."""
    text = " ".join((content or "").split())
    pos = text.lower().find(query.lower())
    if pos < 0:
        return text[:width]
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    out = text[start:end]
    return ("…" if start > 0 else "") + out + ("…" if end < len(text) else "")


async def search_messages(
    user_id: int, query: str, limit: int = 40
) -> list[dict[str, object]]:
    """T29 — full-text-ish search across the user's chat messages. Returns
    recent-first matches with the session title + an excerpt, for the
    in-UI chat search box."""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    # Escape LIKE wildcards so a literal % or _ in the query is matched.
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT m.id AS message_id, m.session_id, m.role, m.content, "
            "       m.created_at, s.title AS session_title "
            "FROM chat_message m "
            "JOIN chat_session s ON s.id = m.session_id "
            "WHERE s.user_id = ? AND m.content LIKE ? ESCAPE '\\' "
            "ORDER BY m.id DESC LIMIT ?",
            (user_id, like, max(1, min(100, int(limit)))),
        )
        rows = await cur.fetchall()
    return [
        {
            "message_id": int(r["message_id"]),
            "session_id": int(r["session_id"]),
            "role": str(r["role"]),
            "session_title": str(r["session_title"]),
            "created_at": str(r["created_at"]),
            "snippet": _snippet(str(r["content"]), q),
        }
        for r in rows
    ]


_RECALL_STOP: frozenset[str] = frozenset({
    "что", "как", "кто", "такой", "такая", "такое", "это", "этот", "эта",
    "мне", "меня", "мой", "моя", "мои", "тебе", "тебя", "про", "для", "или",
    "был", "была", "быть", "есть", "где", "когда", "почему", "зачем", "чем",
    "себя", "свой", "своя", "расскажи", "скажи", "знаешь", "помнишь", "можешь",
    "пожалуйста", "привет", "вообще", "просто", "что-то", "кстати",
})


def _recall_terms(question: str) -> list[str]:
    """Ключевые слова из вопроса: имена собственные (с большой буквы) и
    длинные слова. По ним ищем релевантные прошлые сообщения."""
    import re  # noqa: PLC0415

    terms: list[str] = []
    for w in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9-]+", question or ""):
        lw = w.lower()
        proper = w[:1].isupper() and not w.isupper()  # «Олег», не «ОЛЕГ»/«олег»
        if lw in _RECALL_STOP and not proper:
            continue
        if (proper or len(lw) >= 5) and lw not in terms:
            terms.append(lw)
    return terms[:6]


async def recall_by_terms(
    user_id: int,
    terms: list[str],
    exclude_session_id: int | None = None,
    limit: int = 6,
) -> str:
    """Поиск сообщений по готовому списку терминов (для keyword- и smart-
    режимов). Возвращает готовый блок для системного промпта."""
    terms = [t.strip().lower() for t in terms if t and t.strip()][:10]
    if not terms:
        return ""
    where = " OR ".join(["lower(m.content) LIKE ?"] * len(terms))
    params: list[Any] = [user_id, *[f"%{t}%" for t in terms]]
    sql = (
        "SELECT m.content, m.role, m.created_at, s.title "
        "FROM chat_message m JOIN chat_session s ON s.id = m.session_id "
        f"WHERE s.user_id = ? AND ({where}) "
    )
    if exclude_session_id is not None:
        sql += "AND m.session_id != ? "
        params.append(exclude_session_id)
    sql += "ORDER BY m.id DESC LIMIT 80"
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
    scored: list[tuple[int, Any]] = []
    for r in rows:
        cl = (r["content"] or "").lower()
        hits = sum(1 for t in terms if t in cl)
        if hits:
            scored.append((hits, r))
    scored.sort(key=lambda x: -x[0])
    out: list[str] = []
    seen: set[str] = set()
    for _hits, r in scored[:limit]:
        txt = " ".join((r["content"] or "").split())
        if len(txt) < 3:
            continue
        key = txt[:80]
        if key in seen:
            continue
        seen.add(key)
        who = "Ты" if r["role"] == "user" else "Persona"
        title = (r["title"] or "чат")
        out.append(f"• [{(r['created_at'] or '')[:10]} · «{title[:24]}»] {who}: {txt[:280]}")
    return "\n".join(out)


async def recall_relevant(
    user_id: int, question: str, exclude_session_id: int | None = None, limit: int = 6
) -> str:
    """Режим «по ключевым словам»: термины из вопроса (имена/длинные слова)."""
    return await recall_by_terms(
        user_id, _recall_terms(question), exclude_session_id, limit
    )


async def set_message_pinned(message_id: int, pinned: bool) -> None:
    """T29 — pin/unpin a message so it stays in the chat context even after
    the history is trimmed by volume."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE chat_message SET is_pinned = ? WHERE id = ?",
            (1 if pinned else 0, message_id),
        )
        await conn.commit()


async def set_reaction(message_id: int, user_id: int | None, reaction: str) -> None:
    """T30 — поставить/снять реакцию на сообщение (одна на юзера, toggle).

    Пустая reaction удаляет. ИИ учитывает последнюю реакцию в контексте.
    """
    async with get_connection() as conn:
        if not reaction:
            await conn.execute(
                "DELETE FROM chat_reaction WHERE message_id = ? AND user_id IS ?",
                (message_id, user_id),
            )
        else:
            await conn.execute(
                "INSERT INTO chat_reaction (message_id, user_id, reaction) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(message_id, user_id) DO UPDATE SET "
                "  reaction = excluded.reaction, created_at = datetime('now')",
                (message_id, user_id, reaction),
            )
        await conn.commit()


async def latest_reaction(session_id: int) -> str:
    """Последняя реакция на ответ ассистента в сессии (для подсказки ИИ)."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT cr.reaction FROM chat_reaction cr "
            "JOIN chat_message m ON m.id = cr.message_id "
            "WHERE m.session_id = ? AND m.role = 'assistant' "
            "ORDER BY cr.id DESC LIMIT 1",
            (session_id,),
        )
        row = await cur.fetchone()
    return str(row["reaction"]) if row and row["reaction"] else ""


async def get_pinned_messages(session_id: int, limit: int = 20) -> list[dict[str, str]]:
    """Pinned messages for a session, oldest-first, as {role, content}."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT role, content FROM chat_message "
            "WHERE session_id = ? AND is_pinned = 1 ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        )
        return [{"role": str(r["role"]), "content": str(r["content"])} for r in await cur.fetchall()]


async def add_span_rating(
    asst_message_id: int, session_id: int, selected_text: str, rating: int
) -> None:
    """T29 — record a like/dislike on a selected fragment of an answer."""
    text = (selected_text or "").strip()[:4000]
    if not text or rating not in (-1, 0, 1):
        return
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO training_dataset_span_rating "
            "  (asst_message_id, session_id, selected_text, rating) VALUES (?, ?, ?, ?)",
            (asst_message_id, session_id, text, rating),
        )
        await conn.commit()


async def get_span_ratings(session_id: int) -> dict[int, list[dict[str, object]]]:
    """T31 — спан-рейтинги по сессии: {message_id: [{text, rating}, ...]}.
    Берём последнюю оценку для каждого уникального фрагмента (текст+сообщение)."""
    out: dict[int, list[dict[str, object]]] = {}
    async with get_connection() as conn:
        # Берём ПОСЛЕДНЮЮ запись для каждого (сообщение+фрагмент). Если последняя
        # оценка = 0 (снятие) — фрагмент не подсвечиваем. Так toggle/снятие
        # работают: новая строка rating=0 перекрывает прежний лайк/дизлайк.
        cursor = await conn.execute(
            "SELECT asst_message_id, selected_text, rating FROM training_dataset_span_rating "
            "WHERE id IN ("
            "  SELECT MAX(id) FROM training_dataset_span_rating "
            "  WHERE session_id = ? GROUP BY asst_message_id, selected_text"
            ") AND rating IN (-1, 1) "
            "ORDER BY id ASC",
            (session_id,),
        )
        for row in await cursor.fetchall():
            mid = int(row["asst_message_id"])
            out.setdefault(mid, []).append(
                {"text": str(row["selected_text"]), "rating": int(row["rating"])}
            )
    return out


async def get_streaming_message(session_id: int) -> ChatMessage | None:
    """T29 — the most recent still-streaming assistant message, or None."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM chat_message "
            "WHERE session_id = ? AND is_streaming = 1 "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        row = await cursor.fetchone()
    return _row_to_message(row) if row is not None else None


async def rename_session(user_id: int, session_id: int, title: str) -> bool:
    """Change the sidebar label. Returns False when the row doesn't belong
    to the caller."""
    clean = (title or "").strip()
    if not clean:
        return False
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE chat_session SET title = ?, updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?",
            (clean[:120], session_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def update_session_model(
    user_id: int,
    session_id: int,
    provider: str | None,
    model: str | None,
) -> bool:
    """Pin a provider+model to this session. Used when the user switches
    models mid-conversation — subsequent /ask turns in the same thread
    use the new settings, history stays untouched."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE chat_session SET provider = ?, model = ?, "
            "                        updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?",
            (provider, model, session_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def delete_session(user_id: int, session_id: int) -> bool:
    """Wipe one thread. ``ON DELETE CASCADE`` clears the message rows."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM chat_session WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def touch_session(user_id: int, session_id: int) -> None:
    """Update ``updated_at`` without changing anything else — used when
    a derived title (from first user message) gets assigned."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT title FROM chat_session "
            "WHERE id = ? AND user_id = ? AND title = 'Без названия' LIMIT 1",
            (session_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        cursor = await conn.execute(
            "SELECT content FROM chat_message "
            "WHERE session_id = ? AND role = 'user' "
            "ORDER BY id ASC LIMIT 1",
            (session_id,),
        )
        first = await cursor.fetchone()
        if first is None:
            return
        await conn.execute(
            "UPDATE chat_session SET title = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (_derive_title(str(first["content"]))[:120], session_id),
        )
        await conn.commit()


async def build_history_for_llm(
    session_id: int, max_turns: int = _DEFAULT_HISTORY_TURNS
) -> list[dict[str, str]]:
    """Return the last ``max_turns`` exchanges as OpenAI-compatible
    ``[{"role": ..., "content": ...}, ...]``.

    T21: also returns the running summary as a synthetic ``system``
    message prepended to the list. So the model sees:
        [
          {role: system, content: "Сводка прошлого: ..."},
          {role: user, content: "..."},
          {role: assistant, content: "..."},
          ...recent turns...
        ]
    The route layer can choose to inline this into ``request.system`` or
    pass it as a message list when the provider supports it.

    Effectively: unlimited memory. The summary keeps the gist of
    everything older than the recent N messages; the recent N stay
    verbatim. New messages → summariser rolls them into the summary
    on the next push past N.
    """
    safe_n = max(1, min(500, int(max_turns)))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT summary FROM chat_session WHERE id = ?", (session_id,)
        )
        sess_row = await cursor.fetchone()
        summary = (
            str(sess_row["summary"])
            if sess_row is not None and sess_row["summary"] is not None
            else None
        )
        cursor = await conn.execute(
            "SELECT role, content FROM chat_message "
            "WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, safe_n * 2),  # × 2 for user+assistant pairs
        )
        rows = await cursor.fetchall()

    history: list[dict[str, str]] = []
    if summary:
        history.append({
            "role": "system",
            "content": f"Сводка более ранних сообщений в беседе:\n{summary}",
        })
    history.extend(
        {"role": str(r["role"]), "content": str(r["content"])}
        for r in reversed(rows)
    )
    return history


# --- T21 unlimited memory: auto-summariser -------------------------------

# When a session grows past this, we roll the oldest into a summary and
# keep this many recent messages verbatim.
# T29 — compact sooner so input stays small: once a session passes 30
# messages, fold all but the most recent 16 into the rolling summary.
_SUMMARY_TRIGGER_MESSAGES = 30
_SUMMARY_KEEP_RECENT = 16


async def maybe_summarise(session_id: int) -> bool:
    """If session has > _SUMMARY_TRIGGER_MESSAGES messages, take the
    oldest (count - _SUMMARY_KEEP_RECENT) and ask the LLM to summarise
    them, merging with any existing summary.

    Returns True if a summary was written. Designed to be called after
    each new assistant message — cheap when below threshold (one count
    query), expensive only when actually summarising.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM chat_message WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        total = int(row["n"] or 0) if row is not None else 0
        if total <= _SUMMARY_TRIGGER_MESSAGES:
            return False

        cursor = await conn.execute(
            "SELECT summary, summary_up_to_id FROM chat_session WHERE id = ?",
            (session_id,),
        )
        sess = await cursor.fetchone()
        existing_summary = (
            str(sess["summary"]) if sess and sess["summary"] is not None else ""
        )
        existing_up_to = int(sess["summary_up_to_id"] or 0) if sess else 0

        # Pull messages newer than the existing summary watermark, up
        # to the cutoff (the ones we want to FOLD into the summary).
        cursor = await conn.execute(
            "SELECT id, role, content FROM chat_message "
            "WHERE session_id = ? AND id > ? "
            "ORDER BY id ASC "
            "LIMIT ?",
            (session_id, existing_up_to, total - _SUMMARY_KEEP_RECENT),
        )
        to_summarise = await cursor.fetchall()

    if not to_summarise:
        return False

    transcript = "\n".join(
        f"[{r['role']}] {r['content']}" for r in to_summarise
    )
    last_id = int(to_summarise[-1]["id"])

    # Build summarisation prompt. We do it INLINE — local Qwen handles
    # this fast and the cost is one LLM call per ~20 new messages.
    from app.llm.client import (  # noqa: PLC0415 — avoid module import cycle
        CompletionRequest,
        LLMNotConfigured,
        make_client,
    )

    try:
        client = make_client(kind="chat_summary")
    except LLMNotConfigured:
        log.info("chat.summary.skipped_no_llm", session_id=session_id)
        return False

    if existing_summary:
        prompt = (
            "Ниже есть текущая сводка беседы и новые сообщения после "
            "неё. Обнови сводку так, чтобы она оставалась короткой "
            "(до 1500 слов), но включала все ключевые факты из новых "
            "сообщений.\n\n"
            f"Текущая сводка:\n{existing_summary}\n\n"
            f"Новые сообщения:\n{transcript}\n\n"
            "Обновлённая сводка:"
        )
    else:
        prompt = (
            "Кратко суммаризируй эти сообщения беседы. Сохрани все "
            "ключевые факты, имена, даты, темы. Будь сжат (до 1500 "
            "слов).\n\n"
            f"Сообщения:\n{transcript}\n\n"
            "Сводка:"
        )

    try:
        new_summary = await client.complete(
            CompletionRequest(
                system="Ты — суммаризатор переписки.",
                user=prompt,
                max_tokens=2000,
                temperature=0.3,
            ),
        )
    except Exception as exc:
        log.warning("chat.summary.llm_failed", error=str(exc))
        return False

    if not new_summary or not new_summary.strip():
        return False

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE chat_session SET "
            "  summary = ?, summary_up_to_id = ? "
            "WHERE id = ?",
            (new_summary.strip(), last_id, session_id),
        )
        await conn.commit()

    log.info(
        "chat.summary.updated",
        session_id=session_id,
        messages_folded=len(to_summarise),
        summary_chars=len(new_summary),
    )
    return True
