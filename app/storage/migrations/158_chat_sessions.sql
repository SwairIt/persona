-- T11 (2026-06-07) — Persistent chat sessions for /ask.
--
-- The current /ask is amnesiac: every question is a fresh request, the
-- model has no idea what the user said one minute ago. That works for
-- "tell me what I did at 14:00 yesterday" but breaks the experience
-- when the user wants to actually talk through their day, brainstorm,
-- or have a recurring conversation.
--
-- Two tables:
--
-- ``chat_session`` — one row per conversation thread.
--   * ``user_id`` scopes the session to its owner (multi-device sync
--     T6 ready — sessions follow the user, not the device).
--   * ``title`` — short label shown in the sidebar. Auto-generated from
--     the first message on creation, can be renamed.
--   * ``provider`` / ``model`` — what the session is currently set to
--     use. Each subsequent /ask call against the same session picks
--     them up; the user can swap models mid-conversation and the chain
--     keeps the FULL history (it's just text — see ``chat_message``).
--   * ``created_at`` / ``updated_at`` — for the "recent chats" sort.
--
-- ``chat_message`` — append-only log of (role, content) per session.
--   * ``role`` is ``user``, ``assistant``, or ``system`` (matching the
--     LLM message-list convention).
--   * ``model_used`` is a SNAPSHOT of which model produced the row.
--     Lets the UI hint "this answer was from qwen2.5:3b, switched to
--     gemini for the next one" without parsing logs.
--   * Sort by ``id`` for chronological order — autoincrement gives us
--     monotonic ordering even when system clock jumps.

CREATE TABLE IF NOT EXISTS chat_session (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT 'Без названия',
    provider    TEXT,
    model       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chat_session_user
    ON chat_session(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_message (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT NOT NULL,
    model_used  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chat_message_session
    ON chat_message(session_id, id);
