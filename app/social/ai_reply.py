"""ИИ отвечает в личных сообщениях — генерация и оркестрация хода.

Что здесь происходит
--------------------
Пришло сообщение в ветку → :func:`handle_incoming` смотрит настройку ВТОРОГО
участника (того, кому написали) и решает: ничего, черновик или ответ от его
имени. Настройка и квоты — в :mod:`app.social.ai_pref`, доступ к ветке — в
:mod:`app.social.repository`.

ГРАНИЦА КОНТЕКСТА (главное правило файла)
-----------------------------------------
Это переписка с ТРЕТЬИМ ЧЕЛОВЕКОМ, а не личный чат с ассистентом. Поэтому в
промпт попадает РОВНО четыре вещи:

  1. характер ассистента этого пользователя (``get_active_system_prompt``);
  2. последние N сообщений ЭТОЙ ветки — и ничего больше;
  3. имя собеседника (то же, что видно в UI, — маска, если имени нет);
  4. свободная инструкция стиля (``style_note``), которую человек написал сам.

Сюда НЕ ходят: личные чат-сессии (``chat_message``), долговременная память
(``user_memory``), захват экрана/микрофона, заметки, напоминания. Это не
оптимизация, а смысл фичи: собеседник не подписывался на то, чтобы чужой
ассистент подмешивал в разговор личные данные владельца аккаунта. Тест
``tests/test_dm_ai_reply.py::test_prompt_contains_no_private_context``
сажает канарейки во все эти источники и проверяет их отсутствие.

ЧЕСТНОСТЬ ПЕРЕД СОБЕСЕДНИКОМ
----------------------------
Авто-ответ всегда пишется с ``kind='ai'`` — и у отправителя, и у получателя
он рисуется меткой «✨ ответил ИИ». Никакого режима «незаметно ответить за
меня» здесь нет и не должно появиться.

ИИ НИКОГДА НЕ ОТВЕЧАЕТ ИИ
-------------------------
Триггерное сообщение перечитывается из БД (а не берётся из аргументов), и
``kind='ai'`` останавливает ход. Без этого два включённых ассистента
разговаривали бы друг с другом до упора квоты — и это было бы «работает
как задумано» ровно до первого счёта за токены.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from app.chat.prompts import get_active_system_prompt
from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.social import ai_pref, notifications
from app.social.repository import (
    ThreadAccessError,
    _card_name,
    _require_thread_member,
    list_messages,
    send_message,
)
from app.storage.db import get_connection

if TYPE_CHECKING:
    from datetime import datetime

log = get_logger("persona.social.ai_reply")

#: Сколько последних сообщений ветки уходит в промпт. Двенадцать — это
#: «текущий разговор», а не «вся история отношений»: длиннее ничего не
#: улучшает, но заметно повышает шанс, что модель вытащит из архива фразу,
#: которую человек давно забыл и не имел в виду.
CONTEXT_MESSAGES = 12
#: Потолок длины одной реплики в контексте (обрезаем хвост).
CONTEXT_CHARS = 500
#: Потолок длины сгенерированного ответа.
MAX_REPLY_CHARS = 900

#: Инструкция «пишешь ОТ ИМЕНИ». Отдельная константа, потому что это и есть
#: содержательное отличие от обычного чата — её должно быть видно и легко
#: проверять глазами в ревью.
_ON_BEHALF = """
[Ты пишешь ОТ ИМЕНИ другого человека]
Ты отвечаешь в личной переписке ВМЕСТО {owner}. Собеседника зовут {peer}.
Пиши от первого лица, как написал бы сам {owner}: тот же язык, тот же
уровень формальности, короткими живыми фразами.

Жёсткие правила:
* НИКОГДА не бери на себя обязательства за {owner}: не назначай и не
  подтверждай встречи, не называй суммы, сроки и цены, не обещай что-то
  сделать, не соглашайся на просьбы, у которых есть цена.
* Если не знаешь ответа или вопрос требует решения {owner} — так и напиши:
  «спрошу у него и вернусь» (или «уточню и отвечу» — по-человечески, в тон
  переписке). Это лучший ответ, а не отговорка.
* Не выдумывай факты о {owner}: его планы, местоположение, состояние,
  договорённости. Знаешь только то, что есть в этой переписке.
* Ответ — ОДНО короткое сообщение, без подписи, без кавычек, без
  пояснений о том, что ты ИИ. Собеседник и так увидит пометку.
""".strip()

_STYLE_BLOCK = "\n\n[Пожелание {owner} к стилю ответов]\n{note}"


class ReplyOutcome(TypedDict):
    """Что случилось на этом входящем сообщении (возврат для тестов/логов)."""

    action: str  # 'none' | 'draft' | 'auto' | 'error'
    reason: str
    responder_id: int
    text: str


def _outcome(
    action: str, reason: str, responder_id: int = 0, text: str = ""
) -> ReplyOutcome:
    return {
        "action": action,
        "reason": reason,
        "responder_id": int(responder_id),
        "text": text,
    }


# ── Промпт ──────────────────────────────────────────────────────────────────


def _render_transcript(messages: list[dict[str, Any]], owner: str, peer: str) -> str:
    """Последние сообщения ветки в виде «Кто: что».

    ``mine`` считается с точки зрения ОТВЕЧАЮЩЕГО (``list_messages`` вызван
    под его id), поэтому его собственные реплики подписаны его именем, а не
    «ассистент» — модель должна видеть, как этот человек реально пишет.
    """
    lines: list[str] = []
    for message in messages:
        who = owner if message.get("mine") else peer
        body = str(message.get("body") or "").strip()[:CONTEXT_CHARS]
        if not body:
            continue
        mark = " (ответил ИИ)" if str(message.get("kind")) == "ai" else ""
        lines.append(f"{who}{mark}: {body}")
    return "\n".join(lines)


async def build_prompt(
    responder_id: int,
    thread_id: int,
    peer_name: str,
    owner_name: str,
    style_note: str = "",
) -> CompletionRequest:
    """Собрать запрос к модели. ТОЛЬКО четыре источника — см. шапку модуля.

    ``list_messages`` вызывается под id отвечающего, то есть проходит через
    ``_require_thread_member``: если дружбы уже нет, промпт не соберётся
    вовсе — и это правильный порядок (сначала право читать, потом чтение).
    """
    character = (await get_active_system_prompt(user_id=int(responder_id))).strip()
    system = character + "\n\n" + _ON_BEHALF.format(owner=owner_name, peer=peer_name)
    note = (style_note or "").strip()
    if note:
        system += _STYLE_BLOCK.format(owner=owner_name, note=note)

    messages = await list_messages(
        int(thread_id), int(responder_id), limit=CONTEXT_MESSAGES
    )
    transcript = _render_transcript(messages, owner_name, peer_name)
    user = (
        f"Переписка (последние сообщения):\n{transcript}\n\n"
        f"Напиши ОДНО следующее сообщение от имени {owner_name} для {peer_name}. "
        "Только текст сообщения."
    )
    return CompletionRequest(system=system, user=user, max_tokens=400, temperature=0.7)


def _clean_reply(raw: str) -> str:
    """Убрать обрамляющие кавычки/префиксы, которые любят добавлять модели."""
    text = (raw or "").strip()
    for prefix in ("Ответ:", "Сообщение:", "Reply:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix) :].strip()
    if len(text) >= 2 and text[0] in "«\"'" and text[-1] in "»\"'":
        text = text[1:-1].strip()
    return text[:MAX_REPLY_CHARS]


async def generate_reply(request: CompletionRequest, responder_id: int) -> str:
    """Вызвать модель ОТВЕЧАЮЩЕГО. ``LLMNotConfigured`` пробрасываем наверх.

    Ключ платит тот, кто включил фичу: ``user_id`` — всегда отвечающий,
    никогда собеседник и никогда владелец инстанса.
    """
    client = make_client(kind="dm_reply", user_id=int(responder_id))
    return _clean_reply(await client.complete(request))


# ── Оркестрация хода ────────────────────────────────────────────────────────


async def _load_trigger(thread_id: int, message_id: int) -> dict[str, Any] | None:
    """Перечитать триггерное сообщение из БД.

    Именно из БД, а не из аргументов вызова: ``kind`` — это то, на чём
    держится «ИИ не отвечает ИИ», и брать его на веру от вызывающего кода
    (сегодня — роут, завтра — что угодно) нельзя.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT sender_id, kind, body FROM dm_message WHERE id = ? AND thread_id = ?",
            (int(message_id), int(thread_id)),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "sender_id": int(row["sender_id"]),
        "kind": str(row["kind"] or "human"),
        "body": str(row["body"] or ""),
    }


async def _names(responder_id: int, peer_id: int) -> tuple[str, str]:
    """Отображаемые имена обоих (та же маска e-mail, что и в UI)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, email, display_name FROM users WHERE id IN (?, ?)",
            (int(responder_id), int(peer_id)),
        )
        rows = {int(r["id"]): r for r in await cursor.fetchall()}
    def name_of(uid: int) -> str:
        row = rows.get(int(uid))
        return _card_name(row["display_name"], str(row["email"])) if row else "друг"

    return name_of(responder_id), name_of(peer_id)


async def handle_incoming(  # noqa: PLR0911 — цепочка стражей, см. ниже
    thread_id: int, message_id: int, now: datetime | None = None
) -> ReplyOutcome:
    """Главный вход: решить и выполнить ход ИИ на входящем сообщении.

    Много ``return``'ов здесь — это НЕ разросшаяся функция, а список
    условий, при которых от имени человека НЕ будет отправлено сообщение.
    Каждый выход подписан своей причиной. Свернуть их во вложенные ветки
    можно, но тогда «почему ИИ промолчал» перестанет читаться сверху вниз
    одним проходом — а именно это здесь и надо уметь проверять глазами.

    Никогда не бросает наружу ничего, кроме программных ошибок: всё, что
    может пойти не так у пользователя (не настроена модель, провайдер
    отвалился, дружбу разорвали), превращается в ``action='none'`` или
    ``'error'`` с причиной — фон не имеет права ронять запрос.
    """
    moment = now or ai_pref.utcnow()

    trigger = await _load_trigger(thread_id, message_id)
    if trigger is None:
        return _outcome("none", "no_message")

    # ── Инвариант: ИИ НИКОГДА не отвечает на сообщение ИИ ──────────────────
    if trigger["kind"] == "ai":
        log.debug("social.ai_reply.skip_ai_message", thread_id=int(thread_id))
        return _outcome("none", "peer_is_ai")

    sender_id = trigger["sender_id"]
    try:
        async with get_connection() as conn:
            # ЕДИНСТВЕННЫЙ резолвер доступа: заодно подтверждает, что автор
            # сообщения всё ещё участник ветки и что эти двое всё ещё друзья.
            responder_id = await _require_thread_member(conn, int(thread_id), sender_id)
    except ThreadAccessError:
        return _outcome("none", "thread_closed")

    pref = await ai_pref.get_pref(responder_id, sender_id)
    decision = ai_pref.resolve_action(pref, moment)
    if decision["action"] == "off":
        return _outcome("none", decision["reason"], responder_id)

    owner_name, peer_name = await _names(responder_id, sender_id)

    try:
        request = await build_prompt(
            responder_id,
            int(thread_id),
            peer_name=peer_name,
            owner_name=owner_name,
            style_note=pref["style_note"],
        )
    except ThreadAccessError:
        return _outcome("none", "thread_closed", responder_id)

    try:
        text = await generate_reply(request, responder_id)
    except LLMNotConfigured as exc:
        # Деградация в «ничего» + подсказка в UI. Не 500 и не тихое молчание:
        # человек включил фичу и должен узнать, почему она не сработала.
        await ai_pref.record_error(responder_id, sender_id, str(exc))
        log.info("social.ai_reply.not_configured", user_id=responder_id)
        return _outcome("error", "llm_not_configured", responder_id)
    except Exception as exc:  # noqa: BLE001 — сеть/провайдер/таймаут
        await ai_pref.record_error(responder_id, sender_id, str(exc))
        log.warning("social.ai_reply.failed", user_id=responder_id, error=str(exc)[:200])
        return _outcome("error", "generation_failed", responder_id)

    if not text:
        return _outcome("none", "empty_reply", responder_id)

    if decision["action"] == "draft":
        await ai_pref.save_draft(
            responder_id, int(thread_id), text, reply_to_id=int(message_id), now=moment
        )
        return _outcome("draft", decision["reason"], responder_id, text)

    # ── auto: пишем ОТ ИМЕНИ человека, всегда с меткой kind='ai' ───────────
    try:
        await send_message(int(thread_id), responder_id, text, kind="ai")
    except ThreadAccessError:
        return _outcome("none", "thread_closed", responder_id)
    await ai_pref.record_auto_reply(responder_id, sender_id, moment)
    # Черновик больше не актуален: ответ уже ушёл.
    await ai_pref.clear_draft(responder_id, int(thread_id))

    await notify_ai_replied(responder_id, peer_name, int(thread_id), text, now=moment)
    await notify_new_dm(
        sender_id, owner_name, int(thread_id), text, kind="ai", now=moment
    )
    log.info("social.ai_reply.sent", user_id=responder_id, thread_id=int(thread_id))
    return _outcome("auto", "ok", responder_id, text)


async def notify_incoming(thread_id: int, message_id: int) -> dict[str, str]:
    """Уведомить ПОЛУЧАТЕЛЯ о новом сообщении (по его собственным каналам)."""
    trigger = await _load_trigger(thread_id, message_id)
    if trigger is None:
        return {}
    sender_id = trigger["sender_id"]
    try:
        async with get_connection() as conn:
            recipient_id = await _require_thread_member(conn, int(thread_id), sender_id)
    except ThreadAccessError:
        return {}
    sender_name, _peer = await _names(sender_id, recipient_id)
    return await notify_new_dm(
        recipient_id,
        sender_name,
        int(thread_id),
        trigger["body"],
        kind=trigger["kind"],
    )


async def dispatch(thread_id: int, message_id: int) -> None:
    """Обёртка «выстрелил и забыл» для ``asyncio.create_task`` из роута.

    Тот же приём, что у ``_bg_*`` в ``app/web/routes/chat_sessions.py``:
    отдельного воркер-процесса ради одного LLM-вызова заводить незачем, а
    ронять HTTP-ответ фоновая задача не должна ни при каких обстоятельствах.

    Уведомление и ИИ-ход — две НЕЗАВИСИМЫЕ попытки: упавший SMTP не должен
    отменять ответ ассистента, а неотвечающий провайдер модели — гасить
    уведомление о том, что тебе вообще написали.
    """
    try:
        await notify_incoming(int(thread_id), int(message_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("social.notify.dispatch_failed", error=str(exc)[:200])
    try:
        await handle_incoming(int(thread_id), int(message_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("social.ai_reply.dispatch_failed", error=str(exc)[:200])


# ── Уведомления, привязанные к личным сообщениям ────────────────────────────


async def notify_new_dm(
    recipient_id: int,
    sender_name: str,
    thread_id: int,
    body: str,
    kind: str = "human",
    now: datetime | None = None,
) -> dict[str, str]:
    """«Тебе написал X». Антиспам почты — по ВЕТКЕ (``dm:<thread_id>``)."""
    mark = " ✨ (ответил ИИ)" if kind == "ai" else ""
    return await notifications.notify(
        int(recipient_id),
        "dm_message",
        title=f"Сообщение от {sender_name}{mark}",
        body=(body or "").strip()[:300],
        url=f"/messages/{int(thread_id)}",
        scope=f"dm:{int(thread_id)}",
        now=now,
    )


async def notify_ai_replied(
    user_id: int,
    peer_name: str,
    thread_id: int,
    body: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """«Твой ИИ ответил за тебя в переписке с X» — владельцу настройки.

    Отдельное событие, а не разновидность ``dm_message``: человек должен
    иметь возможность выключить уведомления о чужих сообщениях, но
    оставить включённым «что ушло от моего имени» — это не про удобство,
    а про контроль.
    """
    return await notifications.notify(
        int(user_id),
        "ai_replied",
        title=f"Твой ИИ ответил за тебя — {peer_name}",
        body=(body or "").strip()[:300],
        url=f"/messages/{int(thread_id)}",
        scope=f"ai:{int(thread_id)}",
        now=now,
    )


__all__ = [
    "CONTEXT_MESSAGES",
    "MAX_REPLY_CHARS",
    "ReplyOutcome",
    "build_prompt",
    "dispatch",
    "generate_reply",
    "handle_incoming",
    "notify_ai_replied",
    "notify_incoming",
    "notify_new_dm",
]
