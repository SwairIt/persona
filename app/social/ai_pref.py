"""Настройка «ИИ отвечает за меня» по паре (я, собеседник) + черновики.

Здесь живёт ВЕСЬ SQL этой темы (роуты его не видят — см. архитектурный
гейт ``tests/test_architecture_gates.py``). Модуль намеренно ничего не
знает ни про LLM, ни про HTTP: он только хранит выбор человека, считает
дневную квоту и держит черновик, который виден ТОЛЬКО его владельцу.

Инварианты, которые обеспечивает именно этот модуль
---------------------------------------------------
* дефолт — ``off``. Отсутствие строки и есть «выключено»: включение
  всегда явный акт человека, а не побочный эффект чего-либо;
* ``auto`` требует ``auto_ack=1`` (осознанное согласие «ИИ будет писать
  от моего имени, и собеседник это увидит»). Проверку делает
  :func:`resolve_action`, а не UI: галочка в форме — это удобство, а
  инвариант должен держаться на сервере;
* дневная квота и минимальный интервал между ИИ-ответами превращают
  ``auto`` в ``draft``, а НЕ в «ничего». Молчание выглядело бы как
  поломка; черновик — честная деградация: человек допишет сам.

Время всегда приходит снаружи (``now``) — иначе ни квоту, ни кулдаун
нельзя проверить в тестах без ``sleep``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

from app.logging_setup import get_logger
from app.member_crypto import decrypt_for_thread, encrypt_for_thread
from app.storage.db import get_connection, write_transaction

log = get_logger("persona.social.ai_pref")

Mode = Literal["off", "draft", "auto"]

MODES: tuple[Mode, ...] = ("off", "draft", "auto")

#: Дефолтная дневная квота ИИ-ответов на ОДНУ переписку.
DEFAULT_QUOTA_DAILY = 20
#: Потолок, который человек может выставить руками. Не «сколько угодно»:
#: авто-режим пишет от его имени, и цена ошибки настройки — чужие глаза.
MAX_QUOTA_DAILY = 200
#: Минимальный интервал между двумя ИИ-ответами в одной переписке (сек).
#: Прямая защита от пинг-понга и от «ИИ строчит быстрее человека».
MIN_INTERVAL_SECONDS = 60
#: Потолок длины пользовательской инструкции стиля.
MAX_STYLE_NOTE_CHARS = 400

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


class AIPref(TypedDict):
    """Настройка ИИ-ответов для пары (user_id → peer_id)."""

    user_id: int
    peer_id: int
    mode: Mode
    style_note: str
    quota_daily: int
    used_today: int
    day: str
    last_reply_at: str
    auto_ack: bool
    last_error: str


def utcnow() -> datetime:
    """Единая точка времени модуля (тесты подменяют вызывающую сторону)."""
    return datetime.now(UTC)


def fmt(moment: datetime) -> str:
    """UTC-отметка в формате ``datetime('now')`` SQLite (сравнима строкой)."""
    return moment.astimezone(UTC).strftime(_TS_FORMAT)


def _parse(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, _TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _clean_mode(value: object) -> Mode:
    text = str(value or "").strip().lower()
    return text if text in MODES else "off"  # type: ignore[return-value]


def _default_pref(user_id: int, peer_id: int) -> AIPref:
    return {
        "user_id": int(user_id),
        "peer_id": int(peer_id),
        "mode": "off",
        "style_note": "",
        "quota_daily": DEFAULT_QUOTA_DAILY,
        "used_today": 0,
        "day": "",
        "last_reply_at": "",
        "auto_ack": False,
        "last_error": "",
    }


def _row_to_pref(row: Any, user_id: int, peer_id: int) -> AIPref:
    return {
        "user_id": int(user_id),
        "peer_id": int(peer_id),
        "mode": _clean_mode(row["mode"]),
        "style_note": str(row["style_note"] or ""),
        "quota_daily": int(row["quota_daily"] or 0),
        "used_today": int(row["used_today"] or 0),
        "day": str(row["day"] or ""),
        "last_reply_at": str(row["last_reply_at"] or ""),
        "auto_ack": int(row["auto_ack"] or 0) == 1,
        "last_error": str(row["last_error"] or ""),
    }


_SELECT = (
    "SELECT mode, style_note, quota_daily, used_today, day, last_reply_at, "
    "       auto_ack, last_error "
    "  FROM dm_ai_pref WHERE user_id = ? AND peer_id = ?"
)


async def get_pref(user_id: int, peer_id: int) -> AIPref:
    """Настройка пары. Нет строки → выключено (дефолт), а не ошибка."""
    uid, pid = int(user_id), int(peer_id)
    async with get_connection() as conn:
        cursor = await conn.execute(_SELECT, (uid, pid))
        row = await cursor.fetchone()
    return _default_pref(uid, pid) if row is None else _row_to_pref(row, uid, pid)


async def save_pref(
    user_id: int,
    peer_id: int,
    *,
    mode: str,
    style_note: str = "",
    quota_daily: int = DEFAULT_QUOTA_DAILY,
    auto_ack: bool = False,
    now: datetime | None = None,
) -> AIPref:
    """Сохранить выбор человека. Счётчики и кулдаун НЕ трогаем.

    Смена режима не должна обнулять дневной расход: иначе «выключил —
    включил» становилось бы обходом квоты в один клик.

    ``auto`` без ``auto_ack`` физически не сохраняется как ``auto``: если
    согласие не отмечено, режим опускается до ``draft``. Это второй рубеж
    поверх :func:`resolve_action` — чтобы даже кривой вызов из будущего
    кода не смог включить «пишет от моего имени» молча.
    """
    uid, pid = int(user_id), int(peer_id)
    want = _clean_mode(mode)
    ack = bool(auto_ack)
    if want == "auto" and not ack:
        want = "draft"
    note = (style_note or "").strip()[:MAX_STYLE_NOTE_CHARS]
    quota = max(0, min(int(quota_daily), MAX_QUOTA_DAILY))
    stamp = fmt(now or utcnow())

    async with write_transaction() as conn:
        await conn.execute(
            """
            INSERT INTO dm_ai_pref
                (user_id, peer_id, mode, style_note, quota_daily, auto_ack,
                 last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(user_id, peer_id) DO UPDATE SET
                mode        = excluded.mode,
                style_note  = excluded.style_note,
                quota_daily = excluded.quota_daily,
                auto_ack    = excluded.auto_ack,
                last_error  = NULL,
                updated_at  = excluded.updated_at
            """,
            (uid, pid, want, note, quota, 1 if ack else 0, stamp),
        )
    return await get_pref(uid, pid)


async def disable_everywhere(user_id: int, now: datetime | None = None) -> int:
    """Kill-switch: выключить ИИ во ВСЕХ переписках. Возвращает сколько строк.

    Снимаем заодно ``auto_ack``: обратное включение авто-режима должно
    снова потребовать явной галочки, иначе «выключил везде» оставляло бы
    согласие висеть и один клик возвращал бы письмо от моего имени.
    """
    stamp = fmt(now or utcnow())
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "UPDATE dm_ai_pref SET mode = 'off', auto_ack = 0, updated_at = ? "
            "WHERE user_id = ? AND (mode <> 'off' OR auto_ack <> 0)",
            (stamp, int(user_id)),
        )
        return int(cursor.rowcount or 0)


async def list_active(user_id: int, limit: int = 200) -> list[dict[str, Any]]:
    """Все НЕвыключенные настройки человека + имя собеседника.

    Нужен именно этот срез: страница «выключить ИИ везде» обязана
    показывать, ЧТО именно будет выключено, иначе кнопка предлагает
    подтвердить неизвестное.
    """
    from app.social.repository import _card_name  # noqa: PLC0415 — общий пакет

    uid = int(user_id)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT p.peer_id, p.mode, p.style_note, p.quota_daily, p.used_today, "
            "       p.day, p.last_reply_at, p.auto_ack, p.last_error, "
            "       u.email, u.display_name "
            "  FROM dm_ai_pref p JOIN users u ON u.id = p.peer_id "
            " WHERE p.user_id = ? AND p.mode <> 'off' "
            " ORDER BY p.peer_id LIMIT ?",
            (uid, max(1, min(int(limit), 500))),
        )
        rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        pref = dict(_row_to_pref(row, uid, int(row["peer_id"])))
        pref["peer_name"] = _card_name(row["display_name"], str(row["email"]))
        out.append(pref)
    return out


# ── Квота и кулдаун ─────────────────────────────────────────────────────────


class Decision(TypedDict):
    """Что именно разрешено сделать ИИ на этом входящем сообщении."""

    action: Literal["off", "draft", "auto"]
    reason: str


def resolve_action(pref: AIPref, now: datetime) -> Decision:
    """Чистая функция: режим + квота + кулдаун + согласие → что делаем.

    Вынесена из ввода-вывода намеренно — это единственное место, где
    решается «уйдёт ли сообщение от имени человека», и такое решение
    должно быть проверяемо без базы, сети и таймеров.
    """
    mode = pref["mode"]
    if mode == "off":
        return {"action": "off", "reason": "mode_off"}
    if mode == "draft":
        return {"action": "draft", "reason": "mode_draft"}

    # Дальше только auto — и каждый пункт ниже опускает его до draft.
    if not pref["auto_ack"]:
        return {"action": "draft", "reason": "not_acknowledged"}

    today = now.astimezone(UTC).strftime("%Y-%m-%d")
    used = pref["used_today"] if pref["day"] == today else 0
    if used >= pref["quota_daily"]:
        return {"action": "draft", "reason": "daily_cap"}

    last = _parse(pref["last_reply_at"])
    if last is not None:
        elapsed = (now.astimezone(UTC) - last).total_seconds()
        if elapsed < MIN_INTERVAL_SECONDS:
            return {"action": "draft", "reason": "cooldown"}

    return {"action": "auto", "reason": "ok"}


async def record_auto_reply(user_id: int, peer_id: int, now: datetime) -> None:
    """Списать один ИИ-ответ из дневной квоты и взвести кулдаун.

    Инкремент и сброс дня — ОДИН UPDATE с ``CASE``: два запроса («какой
    сейчас день?» и «прибавь») в параллельных обработчиках могли бы дать
    квоте протечь на один ответ.
    """
    today = now.astimezone(UTC).strftime("%Y-%m-%d")
    stamp = fmt(now)
    async with write_transaction() as conn:
        await conn.execute(
            """
            UPDATE dm_ai_pref
               SET used_today = CASE WHEN day = ? THEN used_today + 1 ELSE 1 END,
                   day = ?,
                   last_reply_at = ?,
                   updated_at = ?
             WHERE user_id = ? AND peer_id = ?
            """,
            (today, today, stamp, stamp, int(user_id), int(peer_id)),
        )


async def record_error(user_id: int, peer_id: int, message: str) -> None:
    """Запомнить причину, по которой ход пропущен (подсказка в UI).

    Пишем ТОЛЬКО в существующую строку: если человек ничего не включал,
    заводить ему строку с ошибкой не за что.
    """
    async with write_transaction() as conn:
        await conn.execute(
            "UPDATE dm_ai_pref SET last_error = ? WHERE user_id = ? AND peer_id = ?",
            ((message or "").strip()[:300], int(user_id), int(peer_id)),
        )


# ── Черновик (виден ТОЛЬКО владельцу) ───────────────────────────────────────


class Draft(TypedDict):
    thread_id: int
    body: str
    reply_to_id: int
    created_at: str


async def save_draft(
    user_id: int,
    thread_id: int,
    body: str,
    reply_to_id: int | None = None,
    now: datetime | None = None,
) -> None:
    """Положить/перезаписать черновик. Один на ветку — свежий важнее старого."""
    text = (body or "").strip()
    if not text:
        return
    async with write_transaction() as conn:
        # Черновик — тот же личный текст, что и отправленное сообщение (и часто
        # дословно им становится). Шифруется тем же ключом ВЕТКИ.
        stored = await encrypt_for_thread(int(thread_id), text, conn)
        await conn.execute(
            """
            INSERT INTO dm_ai_draft (user_id, thread_id, body, reply_to_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, thread_id) DO UPDATE SET
                body        = excluded.body,
                reply_to_id = excluded.reply_to_id,
                created_at  = excluded.created_at
            """,
            (
                int(user_id),
                int(thread_id),
                stored,
                int(reply_to_id) if reply_to_id else None,
                fmt(now or utcnow()),
            ),
        )


async def get_draft(user_id: int, thread_id: int) -> Draft | None:
    """Черновик ЭТОГО человека в этой ветке. Чужой недостижим по ключу."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT body, reply_to_id, created_at FROM dm_ai_draft "
            "WHERE user_id = ? AND thread_id = ?",
            (int(user_id), int(thread_id)),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "thread_id": int(thread_id),
        "body": await decrypt_for_thread(int(thread_id), row["body"]),
        "reply_to_id": int(row["reply_to_id"] or 0),
        "created_at": str(row["created_at"] or ""),
    }


async def clear_draft(user_id: int, thread_id: int) -> bool:
    """Убрать черновик («Убрать» в композере, либо после отправки)."""
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "DELETE FROM dm_ai_draft WHERE user_id = ? AND thread_id = ?",
            (int(user_id), int(thread_id)),
        )
        return int(cursor.rowcount or 0) > 0


__all__ = [
    "DEFAULT_QUOTA_DAILY",
    "MAX_QUOTA_DAILY",
    "MAX_STYLE_NOTE_CHARS",
    "MIN_INTERVAL_SECONDS",
    "MODES",
    "AIPref",
    "Decision",
    "Draft",
    "Mode",
    "clear_draft",
    "disable_everywhere",
    "fmt",
    "get_draft",
    "get_pref",
    "list_active",
    "record_auto_reply",
    "record_error",
    "resolve_action",
    "save_draft",
    "save_pref",
    "utcnow",
]
