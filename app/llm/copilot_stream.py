"""Стрим встроенного копилота Persona (SSE-события delta/meta/done).

Слайс B2. Копилот живёт прямо на сайте (панель справа снизу под мастер-флагом
«ИИ везде») и отвечает на три вида запросов:

* ``ask``          — обычный вопрос про Persona / жизнь пользователя. Подмешиваем
                     короткий контекст: recall памяти по всем чатам (если есть).
* ``summary``      — кратко объясни/суммируй текущую страницу (по ``page_url``).
* ``find_setting`` — намерение пользователя → текстом, какую страницу настроек
                     открыть. Сначала пробуем чистую функцию
                     :func:`app.web.routes.settings_hub.search_settings`.

Паттерн SSE полностью повторяет :mod:`app.llm.qa_stream`: генератор yield-ит
dict-ы с ключом ``type`` (``meta`` → 0..N ``delta`` → ``done``), а HTTP-слой
(:mod:`app.web.routes.copilot`) сериализует их в кадры ``data: <json>\\n\\n``.
Ошибку конфигурации LLM отдаём отдельным событием ``{type:'error',
reason:'llm_offline'}`` — сервер не должен 500-ить, если ПК-воркер офлайн.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.llm.client import CompletionRequest, LLMClient, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import set_kv

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.copilot_stream")

#: Единый характер копилота. Коротко, по делу, по-русски — он помогает прямо на
#: странице, а не пишет эссе.
_COPILOT_SYSTEM = (
    "Ты — встроенный копилот Persona, помогаешь пользователю прямо на сайте, "
    "коротко и по делу, по-русски."
)

#: Допустимые режимы. Неизвестный режим тихо трактуем как обычный вопрос,
#: чтобы кривой ?mode= не ронял стрим.
_VALID_MODES = ("ask", "summary", "find_setting")

#: Бюджет ответа — копилот отвечает кратко, длинные простыни тут не нужны.
_MAX_TOKENS = 500
_ENABLE_MARKERS = ("включи", "включить", "активируй", "enable", "turn on")
_DISABLE_MARKERS = ("выключи", "отключи", "disable", "turn off")


async def _recall_context(question: str, user_id: int | None) -> str:
    """Короткий блок памяти по всем чатам пользователя (best-effort).

    Использует keyword-recall (FTS5 bm25 + LIKE-fallback) — тот же путь, что и
    чат по умолчанию. Любой сбой (нет таблиц/пустая память) → пустая строка,
    копилот просто ответит без контекста.
    """
    if not user_id or not question:
        return ""
    try:
        from app.chat.sessions import recall_relevant  # noqa: PLC0415

        return (await recall_relevant(user_id, question, limit=4)) or ""
    except Exception as exc:  # noqa: BLE001 — память опциональна, не роняем стрим
        log.debug("copilot.recall_failed", error=str(exc))
        return ""


def _find_settings_block(question: str) -> str:
    """Топ-совпадения по страницам настроек для режима ``find_setting``.

    Чистая функция :func:`search_settings` не ходит в БД, поэтому зовём её
    синхронно. Возвращаем компактный список «label → href» для подсказки LLM
    (или пустую строку, если ничего не нашлось).
    """
    try:
        from app.web.routes.settings_hub import search_settings  # noqa: PLC0415

        results = search_settings(question, limit=6)
    except Exception as exc:  # noqa: BLE001 — поиск опционален
        log.debug("copilot.search_settings_failed", error=str(exc))
        return ""
    lines = [f"• {r['label']} → {r['href']}" for r in results]
    return "\n".join(lines)


async def _apply_safe_setting_action(
    question: str, user_id: int | None = None
) -> tuple[str, str] | None:
    """Apply a tiny owner-safe allowlist of explicit setting requests.

    «Owner-safe» тут буквально: строки, в которые пишет эта функция
    (``ai_everywhere`` / ``advanced_mode`` / ``feat_tools``) — ГЛОБАЛЬНЫЕ
    kv-флаги владельца, общие для всего инстанса. Раньше гейта не было, и
    любой зарегистрированный пользователь фразой «включи инструменты» в
    копилоте переключал флаги ВЛАДЕЛЬЦА. Теперь не-владельцу действие не
    применяется вовсе (``None``) — копилот просто ответит текстом, а чужие
    настройки останутся как были.
    """
    if user_id is not None:
        try:
            from app.auth.owner import is_owner  # noqa: PLC0415

            if not await is_owner(int(user_id)):
                return None
        except Exception:  # noqa: BLE001 — сбой гейта → ничего не пишем
            return None

    lowered = " ".join(str(question or "").casefold().split())
    enabled: bool | None = None
    if any(marker in lowered for marker in _ENABLE_MARKERS):
        enabled = True
    elif any(marker in lowered for marker in _DISABLE_MARKERS):
        enabled = False
    if enabled is None:
        return None

    key = ""
    label = ""
    href = ""
    if "ии везде" in lowered or "ai everywhere" in lowered:
        key, label, href = "ai_everywhere", "режим «ИИ везде»", "/settings/ai-everywhere"
    elif "расширенн" in lowered:
        key, label, href = "advanced_mode", "расширенный режим", "/settings/advanced"
    elif "инструмент" in lowered:
        key, label, href = "feat_tools", "инструменты Persona", "/settings/advanced"
    if not key:
        return None
    async with get_connection() as conn:
        if key == "feat_tools" and enabled:
            await set_kv(conn, "advanced_mode", "1")
        await set_kv(conn, key, "1" if enabled else "0")
    verb = "Включил" if enabled else "Выключил"
    return f"{verb} {label}.", href


def _build_prompt(question: str, page_url: str, mode: str, extra: str) -> str:
    """Собрать пользовательский промпт под конкретный режим копилота."""
    page_url = (page_url or "").strip()
    if mode == "summary":
        parts = [
            "Пользователь просит кратко объяснить/суммировать текущую страницу "
            "сайта Persona.",
        ]
        if page_url:
            parts.append(f"URL страницы: {page_url}")
        if question:
            parts.append(f"Уточнение пользователя: {question}")
        parts.append(
            "Ответь в 2-4 предложениях: что это за экран и что тут можно "
            "сделать. Без воды."
        )
        return "\n".join(parts)

    if mode == "find_setting":
        parts = [
            "Пользователь хочет найти нужную страницу настроек Persona по "
            "своему намерению.",
            f"Намерение: {question}" if question else "Намерение: (не указано)",
        ]
        if extra:
            parts.append(
                "Подходящие страницы настроек (label → путь):\n" + extra
            )
            parts.append(
                "Выбери самый подходящий пункт и ответь одним-двумя "
                "предложениями: какую страницу открыть и её путь. Если ничего "
                "не подходит — так и скажи."
            )
        else:
            parts.append(
                "Точных совпадений в каталоге настроек не нашлось. Подскажи "
                "по смыслу, куда примерно смотреть, коротко."
            )
        return "\n".join(parts)

    # mode == "ask" (и любой неизвестный режим)
    parts = [f"Вопрос пользователя: {question}"]
    if page_url:
        parts.append(f"(Пользователь сейчас на странице: {page_url})")
    if extra:
        parts.append(
            "Что известно из памяти по прошлым чатам (может пригодиться):\n"
            + extra
        )
    parts.append("Ответь коротко и по делу.")
    return "\n".join(parts)


async def _not_configured_event(
    user_id: int | None, exc: Exception
) -> dict[str, Any]:
    """Кадр ошибки «нет модели», разный для владельца и обычного юзера.

    Владелец — ровно прежний кадр ``reason='llm_offline'`` («ПК-воркер
    офлайн»): его копилот и правда крутится на домашнем ПК. Остальным этот
    текст бессмысленен — им нужен свой провайдер, поэтому отдаём
    ``reason='llm_not_configured'`` со ссылкой на /settings/llm.
    """
    owner = False
    if user_id is not None:
        try:
            from app.auth.owner import is_owner  # noqa: PLC0415

            owner = await is_owner(int(user_id))
        except Exception:  # noqa: BLE001 — сбой гейта не должен ронять стрим
            owner = False
    if user_id is None or owner:
        log.info("copilot.llm_offline", error=str(exc))
        return {
            "type": "error",
            "reason": "llm_offline",
            "message": "ПК-воркер офлайн",
        }
    log.info("copilot.llm_not_configured", error=str(exc))
    return {
        "type": "error",
        "reason": "llm_not_configured",
        "message": "Свой AI не подключён — открой /settings/llm",
    }


async def stream_copilot(
    question: str,
    page_url: str = "",
    mode: str = "ask",
    user_id: int | None = None,
    client: LLMClient | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE-ready dict-ы для одного запроса копилота.

    Порядок событий как в qa_stream: ровно один ``meta`` → 0..N ``delta`` →
    ровно один ``done`` (даже если ответ пустой). При недоступном LLM отдаём
    ``{type:'error', reason:'llm_offline'}`` вместо исключения наружу.
    """
    question = (question or "").strip()
    mode = (mode or "ask").strip().lower()
    if mode not in _VALID_MODES:
        mode = "ask"

    # Собираем режимо-зависимый контекст ДО открытия стрима.
    extra = ""
    if mode == "ask":
        extra = await _recall_context(question, user_id)
    elif mode == "find_setting":
        extra = _find_settings_block(question)

    yield {"type": "meta", "mode": mode, "has_context": bool(extra)}

    action = await _apply_safe_setting_action(question, user_id)
    if action is not None:
        answer, href = action
        yield {"type": "delta", "text": answer}
        yield {
            "type": "done",
            "full_answer": answer,
            "href": href,
            "label": "Открыть настройку",
        }
        return

    prompt = _build_prompt(question, page_url, mode, extra)
    request = CompletionRequest(
        system=_COPILOT_SYSTEM,
        user=prompt,
        max_tokens=_MAX_TOKENS,
    )

    # make_client(kind='copilot') у ВЛАДЕЛЬЦА → провайдер worker (модель на
    # ПК), ключ не нужен. У обычного пользователя ``user_id`` уводит резолв в
    # его собственные настройки: чужой ПК недоступен, нужен свой провайдер.
    # Если LLM не сконфигурирован/воркер офлайн — благородная ошибка.
    try:
        llm = client or make_client(kind="copilot", user_id=user_id)
    except LLMNotConfigured as exc:
        # Разводим два случая: у ВЛАДЕЛЬЦА это «ПК-воркер офлайн» (кадр как
        # был, байт-в-байт), у обычного пользователя — «свой AI не подключён»,
        # что чинится на /settings/llm, а не ожиданием.
        error_event = await _not_configured_event(user_id, exc)
        yield error_event
        return

    chunks: list[str] = []
    try:
        async for delta in llm.stream(request):
            if not delta:
                continue
            chunks.append(delta)
            yield {"type": "delta", "text": delta}
    except LLMNotConfigured as exc:
        # Воркер мог отвалиться уже в процессе стрима (poll-таймаут/офлайн);
        # у не-владельца та же ошибка означает «твой провайдер не отвечает».
        log.info("copilot.llm_offline_mid", error=str(exc))
        yield await _not_configured_event(user_id, exc)
        return
    except Exception as exc:  # noqa: BLE001 — любой сбой стрима → мягкий done
        log.warning("copilot.stream_failed", error=str(exc))
        yield {
            "type": "done",
            "full_answer": "".join(chunks),
            "error": str(exc),
        }
        return

    full_answer = "".join(chunks)
    log.info(
        "copilot.done",
        mode=mode,
        answer_len=len(full_answer),
        has_context=bool(extra),
    )
    done: dict[str, Any] = {"type": "done", "full_answer": full_answer}
    if mode == "find_setting":
        try:
            from app.web.routes.settings_hub import search_settings  # noqa: PLC0415

            settings = search_settings(question, limit=3)
            if settings:
                done["settings"] = settings
        except Exception:
            pass
    yield done


__all__ = ["stream_copilot"]
