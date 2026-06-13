"""Persistent chat sessions for /ask — talks to the LLM with full memory.

Public surface:
  * :func:`create_session`      — new conversation thread
  * :func:`list_sessions`       — sidebar listing
  * :func:`get_session`         — single session + its messages
  * :func:`append_message`      — log a user or assistant turn
  * :func:`rename_session`      — change the sidebar title
  * :func:`delete_session`      — wipe a thread
  * :func:`build_history_for_llm` — last N exchanges in the OpenAI
                                    ``messages`` array shape

All helpers scope by ``user_id`` so a leak between accounts is impossible
at the data layer — the route can pass the session_id from the URL and
the user_id from the auth dependency without further sanitising.
"""

from app.chat.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    PRESETS,
    get_active_system_prompt,
    is_custom_system_prompt,
    reset_active_system_prompt,
    set_active_system_prompt,
)
from app.chat.sessions import (
    add_span_rating,
    append_message,
    build_history_for_llm,
    create_session,
    delete_session,
    finalize_streaming_message,
    get_pinned_messages,
    get_session,
    get_streaming_message,
    list_messages,
    list_sessions,
    maybe_summarise,
    rename_session,
    search_messages,
    set_message_pinned,
    start_streaming_message,
    touch_session,
    update_session_model,
    update_streaming_message,
)

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "PRESETS",
    "add_span_rating",
    "append_message",
    "get_pinned_messages",
    "set_message_pinned",
    "build_history_for_llm",
    "create_session",
    "get_active_system_prompt",
    "is_custom_system_prompt",
    "reset_active_system_prompt",
    "set_active_system_prompt",
    "delete_session",
    "finalize_streaming_message",
    "get_session",
    "get_streaming_message",
    "list_messages",
    "list_sessions",
    "maybe_summarise",
    "rename_session",
    "search_messages",
    "start_streaming_message",
    "touch_session",
    "update_session_model",
    "update_streaming_message",
]
