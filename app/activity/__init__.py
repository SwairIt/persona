"""Окно активности — журнал того, что делает ИИ (вызовы инструментов).

Тонкий слой над таблицей ``tool_execution`` (миграция 181). Чат-цикл
(send-stream) оборачивает каждый вызов инструмента: ``start_execution`` перед
вызовом, ``finish_execution`` после. Параллельно публикуем SSE-фрейм
(``live_sse.publish_activity``), чтобы UI показывал «что делает ИИ» вживую,
и сохраняем для replay по сессии (``list_session_activity``).

Всё best-effort: запись активности НИКОГДА не должна ломать ответ ассистента —
вызывающий оборачивает в try/except, а сами функции возвращают None при сбое.
"""

from __future__ import annotations

from app.activity.store import (
    finish_execution,
    list_recent_activity,
    list_session_activity,
    start_execution,
)

__all__ = [
    "finish_execution",
    "list_recent_activity",
    "list_session_activity",
    "start_execution",
]
