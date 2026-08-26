"""Личная память ассистента — курируемые факты о пользователе.

Отдельный слой от неявного keyword-recall (`recall_relevant`): здесь живут
явные факты, которые пользователь (или ИИ через /remember) сохранил, чтобы
ассистент «помнил кто ты» между чатами. Подмешивается в системный промпт
(закреплённые + недавние) и доступен инструменту `query_memory`.

Таблица: ``user_memory`` (миграция 180). Без внешних зависимостей.

ШИФРОВАНИЕ ТЕКСТА ФАКТОВ (v2.33.x)
----------------------------------
У НЕ-владельца ``user_memory.text`` лежит в базе зашифрованным
(``app/member_crypto.py``, ключ пользователя). У владельца — открытым текстом,
и это осознанно: владелец и есть тот, у кого база, а его строки напрямую
читают SQL-джойны сновидений/проекций/графа (``app/adapters/memory/*``,
``app/adapters/projection/*``, ``app/knowledge_graph.py``) — все owner-only.
Чужих строк там не бывает, поэтому ни один из этих путей не задет.

Почему это вообще получилось дёшево: ВЕСЬ поиск по фактам уже делается в
Python поверх :func:`list_memory` (``search_memory``, ``forget``,
``_candidates``, ``consolidate_memories``) — SQLite ``lower()`` не умеет
кириллицу, поэтому сравнение текста давно вынесено из SQL. Единственным
местом, где текст сравнивался запросом, был дедуп на вставке; он тоже
переехал в Python (см. :func:`_add_memory_in_transaction`).
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.member_crypto import decrypt_for_user, encrypt_for_user, encrypts_memory_for
from app.storage.db import get_connection, write_transaction

log = get_logger("persona.user_memory")

_KINDS = {"fact", "preference", "person", "project", "reminder", "other"}
_MAX_LEN = 600
# Бюджет авто-роста (инсайт Hermes: «без тихого переполнения»). Авто-извлечение
# фактов останавливается при достижении лимита — память не превращается в свалку;
# пользователь чистит её в /settings/memory. Ручной /remember не ограничен.
MEMORY_AUTO_CAP = 80


def _heuristic_importance(text: str, kind: str, pinned: bool = False) -> float:
    """Грубая важность факта 1..10 без LLM (всегда работает, дёшево) — Ф3-C.

    Generative-recall и ночной «сон» используют salience как вес. Durable-факты
    (имена/цели/предпочтения/закреплённые) — выше; смолток/очень короткое — ниже.
    Не LLM специально: дешёвый детерминированный fallback, не зависит от Ollama.
    """
    base = {
        "person": 7.0,
        "project": 7.0,
        "preference": 6.5,
        "reminder": 6.0,
        "fact": 5.0,
        "other": 4.0,
    }.get(kind, 5.0)
    low = (text or "").lower()
    n = len(text or "")
    if n < 15:
        base -= 1.0
    elif n > 80:
        base += 0.7
    if any(
        w in low
        for w in (
            "зовут",
            "люблю",
            "ненавиж",
            "цель",
            "хочу",
            "работа",
            "живу",
            "важно",
            "name",
            "goal",
            "prefer",
            "always",
            "never",
        )
    ):
        base += 0.8
    if any(w in low for w in ("привет", "ладно", "ха-ха", " lol", "hello")):
        base -= 0.8
    if pinned:
        base += 1.5
    return max(1.0, min(10.0, round(base, 1)))


async def add_memory(
    user_id: int,
    text: str,
    kind: str = "fact",
    source_session_id: int | None = None,
    pinned: bool = False,
) -> int | None:
    """Сохранить факт. Дедуп: точное совпадение текста для пользователя не дублируем."""
    text = " ".join((text or "").split())[:_MAX_LEN]
    if not text:
        return None
    if kind not in _KINDS:
        kind = "fact"
    async with write_transaction() as conn:
        return await _add_memory_in_transaction(
            conn,
            user_id,
            text,
            kind=kind,
            source_session_id=source_session_id,
            pinned=pinned,
        )


async def _add_memory_in_transaction(
    conn: Any,
    user_id: int,
    text: str,
    *,
    kind: str,
    source_session_id: int | None,
    pinned: bool = False,
) -> int:
    """Insert/deduplicate one already-normalized fact on the caller transaction."""

    encrypted = await encrypts_memory_for(user_id)

    # Дедуп. У зашифрованного пользователя сравнить тексты запросом нельзя:
    # один и тот же факт даёт разный шифротекст (случайный nonce на запись).
    # Поэтому сравниваем в Python по расшифрованным строкам — это те же
    # десятки строк, что и так читает list_memory, и та же семантика
    # (casefold вместо ASCII-only lower() — для кириллицы даже точнее).
    if encrypted:
        cur = await conn.execute(
            "SELECT id, text FROM user_memory WHERE user_id = ? AND valid_until IS NULL",
            (user_id,),
        )
        target = text.casefold()
        existing = None
        for row in await cur.fetchall():
            plain = await decrypt_for_user(user_id, row["text"], conn)
            if plain.casefold() == target:
                existing = row
                break
    else:
        cur = await conn.execute(
            "SELECT id FROM user_memory WHERE user_id = ? AND lower(text) = lower(?) "
            "AND valid_until IS NULL LIMIT 1",
            (user_id, text),
        )
        existing = await cur.fetchone()
    if existing:
        if pinned:
            await conn.execute(
                "UPDATE user_memory SET pinned = 1, updated_at = datetime('now') WHERE id = ?",
                (existing["id"],),
            )
        return int(existing["id"])
    # Важность считается по ОТКРЫТОМУ тексту — до подмены на шифротекст.
    sal = _heuristic_importance(text, kind, pinned)
    stored = await encrypt_for_user(user_id, text, conn) if encrypted else text
    cur = await conn.execute(
        "INSERT INTO user_memory(user_id, kind, text, pinned, source_session_id, "
        "salience, importance_source) VALUES(?,?,?,?,?,?,?)",
        (
            user_id,
            kind,
            stored,
            1 if pinned else 0,
            source_session_id,
            sal,
            "heuristic",
        ),
    )
    return int(cur.lastrowid)


async def list_memory(
    user_id: int,
    limit: int = 200,
    include_invalidated: bool = False,
    order_by_salience: bool = False,
) -> list[dict[str, Any]]:
    """Факты пользователя: закреплённые сверху, потом новые.

    По умолчанию — только АКТУАЛЬНЫЕ (``valid_until IS NULL``). bi-temporal
    история (устаревшие/опровергнутые факты) доступна через
    ``include_invalidated=True`` (для UI-инспектора памяти).

    ``order_by_salience`` (Ф3-C) — для отбора в системный промпт: при переполнении
    бюджета всплывают ВАЖНЫЕ факты (salience), а не просто свежие. Для UI остаётся
    хронология (pinned + recency).
    """
    where = "user_id = ?" + ("" if include_invalidated else " AND valid_until IS NULL")
    order = (
        "ORDER BY pinned DESC, COALESCE(salience, 5) DESC, id DESC LIMIT ?"
        if order_by_salience
        else "ORDER BY pinned DESC, id DESC LIMIT ?"
    )
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, kind, text, pinned, source_session_id, created_at, "
            "       valid_until, superseded_by "
            f"FROM user_memory WHERE {where} {order}",
            (user_id, max(1, min(1000, int(limit)))),
        )
        rows = await cur.fetchall()
    # ЕДИНСТВЕННАЯ точка расшифровки фактов на чтение: search_memory, forget,
    # _candidates, consolidate_memories и build_memory_block ходят сюда.
    return [
        {
            "id": int(r["id"]),
            "kind": str(r["kind"]),
            "text": await decrypt_for_user(user_id, r["text"]),
            "pinned": bool(r["pinned"]),
            "created_at": str(r["created_at"]),
            "valid_until": r["valid_until"],
            "superseded_by": r["superseded_by"],
        }
        for r in rows
    ]


async def set_pinned(user_id: int, mem_id: int, pinned: bool) -> bool:
    async with write_transaction() as conn:
        cur = await conn.execute(
            "UPDATE user_memory SET pinned = ?, updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?",
            (1 if pinned else 0, mem_id, user_id),
        )
        return cur.rowcount > 0


async def count_memory(user_id: int) -> int:
    """Число АКТУАЛЬНЫХ фактов (для бюджета авто-роста)."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM user_memory WHERE user_id = ? AND valid_until IS NULL",
            (user_id,),
        )
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def invalidate_memory(user_id: int, mem_id: int, superseded_by: int | None = None) -> bool:
    """Soft-invalidate факта: valid_until=now (+superseded_by). НЕ удаляет —
    факт уходит из recall/list, но остаётся в истории и откатывается."""
    async with write_transaction() as conn:
        return await _invalidate_memory_in_transaction(
            conn, user_id, mem_id, superseded_by=superseded_by
        )


async def _invalidate_memory_in_transaction(
    conn: Any,
    user_id: int,
    mem_id: int,
    *,
    superseded_by: int | None = None,
) -> bool:
    """Soft-invalidate one fact on the caller's existing write transaction."""

    cur = await conn.execute(
        "UPDATE user_memory SET valid_until = datetime('now'), superseded_by = ?, "
        "updated_at = datetime('now') "
        "WHERE id = ? AND user_id = ? AND valid_until IS NULL",
        (superseded_by, mem_id, user_id),
    )
    return cur.rowcount > 0


async def edit_memory(user_id: int, mem_id: int, text: str) -> bool:
    """Отредактировать текст факта (ручная правка из инспектора памяти)."""
    text = " ".join((text or "").split())[:_MAX_LEN]
    if not text:
        return False
    async with write_transaction() as conn:
        stored = (
            await encrypt_for_user(user_id, text, conn)
            if await encrypts_memory_for(user_id)
            else text
        )
        cur = await conn.execute(
            "UPDATE user_memory SET text = ?, updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?",
            (stored, mem_id, user_id),
        )
        return cur.rowcount > 0


async def restore_memory(user_id: int, mem_id: int) -> bool:
    """Откат soft-invalidate: вернуть факт в актуальные (valid_until=NULL)."""
    async with write_transaction() as conn:
        cur = await conn.execute(
            "UPDATE user_memory SET valid_until = NULL, superseded_by = NULL, "
            "updated_at = datetime('now') WHERE id = ? AND user_id = ?",
            (mem_id, user_id),
        )
        return cur.rowcount > 0


async def delete_memory(user_id: int, mem_id: int) -> bool:
    async with write_transaction() as conn:
        cur = await conn.execute(
            "DELETE FROM user_memory WHERE id = ? AND user_id = ?", (mem_id, user_id)
        )
        return cur.rowcount > 0


async def forget(user_id: int, query: str) -> int:
    """Забыть по id (если число) или по подстроке текста. → число удалённых.

    Подстрочный матч делаем в Python (casefold), т.к. SQLite lower()/NOCASE
    работают только с ASCII — кириллицу не приводят к нижнему регистру.
    """
    query = (query or "").strip()
    if not query:
        return 0
    if query.isdigit():
        return 1 if await delete_memory(user_id, int(query)) else 0
    qf = query.casefold()
    rows = await list_memory(user_id, limit=1000)
    ids = [r["id"] for r in rows if qf in r["text"].casefold()]
    if not ids:
        return 0
    async with write_transaction() as conn:
        await conn.executemany(
            "DELETE FROM user_memory WHERE id = ? AND user_id = ?",
            [(i, user_id) for i in ids],
        )
    return len(ids)


async def search_memory(user_id: int, query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Поиск по фактам (для query_memory). casefold в Python — корректно для кириллицы."""
    q = (query or "").strip().casefold()
    rows = await list_memory(user_id, limit=500)
    if q:
        rows = [r for r in rows if q in r["text"].casefold()]
    return [
        {"id": r["id"], "kind": r["kind"], "text": r["text"], "pinned": r["pinned"]}
        for r in rows[: max(1, min(50, int(limit)))]
    ]


# mem0-стиль решение по новому факту относительно похожих существующих.
_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["add", "update", "delete", "noop"]},
        "target_id": {"type": ["integer", "null"]},
    },
    "required": ["action"],
}


def _key_tokens(text: str) -> set[str]:
    return {w for w in text.casefold().split() if len(w) >= 4}


async def _candidates(user_id: int, text: str, kind: str, top_k: int = 12) -> list[dict[str, Any]]:
    """Похожие АКТУАЛЬНЫЕ факты (по пересечению ключевых токенов; того же вида приоритетнее)."""
    toks = _key_tokens(text)
    rows = await list_memory(user_id, limit=500)
    scored: list[tuple[int, dict[str, Any]]] = []
    for r in rows:
        overlap = len(toks & _key_tokens(r["text"]))
        if overlap or r["kind"] == kind:
            scored.append((overlap + (1 if r["kind"] == kind else 0), r))
    scored.sort(key=lambda x: -x[0])
    return [r for _s, r in scored[:top_k]]


async def _llm_decide(
    new_text: str,
    candidates: list[dict[str, Any]],
    user_id: int | None = None,
) -> dict[str, Any] | None:
    """Решение ADD/UPDATE/DELETE/NOOP через GBNF (только Ollama). None если LLM нет."""
    if not candidates:
        return None
    try:
        from app.llm.client import CompletionRequest, make_client  # noqa: PLC0415

        client = make_client(kind="chat_summary", user_id=user_id)
    except Exception:  # noqa: BLE001
        return None
    if not hasattr(client, "complete_json"):  # GBNF-схема — только Ollama
        return None
    listing = "\n".join(f"id={c['id']}: {c['text']}" for c in candidates)
    system = (
        "Ты ведёшь долговременную память. Дан НОВЫЙ факт о пользователе и список "
        "СУЩЕСТВУЮЩИХ фактов. Реши ОДНО действие:\n"
        "- noop: новый факт уже есть/неинформативен;\n"
        "- add: новый факт независим (target_id=null);\n"
        "- update: новый факт уточняет/заменяет существующий → укажи target_id;\n"
        "- delete: новый факт ПРОТИВОРЕЧИТ существующему (старое больше не верно) → target_id.\n"
        "Верни строго JSON {action, target_id}."
    )
    user = f"СУЩЕСТВУЮЩИЕ:\n{listing}\n\nНОВЫЙ ФАКТ: {new_text}"
    try:
        out = await client.complete_json(
            CompletionRequest(system=system, user=user, max_tokens=80, temperature=0.0),
            _DECISION_SCHEMA,
        )
        return out if isinstance(out, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.debug("user_memory.decide_failed", error=str(exc))
        return None


async def reconcile_and_add(
    user_id: int,
    text: str,
    kind: str = "fact",
    source_session_id: int | None = None,
    decider: Any = None,
) -> dict[str, Any]:
    """mem0-стиль добавление с разрешением противоречий (bi-temporal soft-invalidate).

    Возвращает {action, id, invalidated?}. Без LLM (или для облачных провайдеров) —
    тихий fallback на обычный add (дедуп по точному тексту). ``decider`` — для тестов
    (async callable (new_text, candidates) -> {action,target_id}).
    """
    text = " ".join((text or "").split())[:_MAX_LEN]
    if not text:
        return {"action": "noop", "id": None}
    if kind not in _KINDS:
        kind = "fact"
    cands = await _candidates(user_id, text, kind)
    # точный дубль среди актуальных → noop
    for c in cands:
        if c["text"].casefold() == text.casefold():
            return {"action": "noop", "id": c["id"]}
    # Дефолтный решатель ходит в LLM — значит ходит КОНФИГОМ ЭТОГО юзера
    # (у не-владельца свой провайдер/ключ). Тестовые ``decider``-ы остаются
    # двухаргументными, поэтому user_id прокидываем только в свой дефолт.
    if decider is not None:
        decision = await decider(text, cands)
    else:
        decision = await _llm_decide(text, cands, user_id=user_id)
    action = str((decision or {}).get("action") or "add").lower()
    target = (decision or {}).get("target_id")
    valid_targets = {c["id"] for c in cands}
    if action == "noop":
        return {"action": "noop", "id": None}
    if action in ("update", "delete") and target in valid_targets:
        # и update, и delete: добавляем новый факт + soft-invalidate старый
        # (delete = старое опровергнуто, но новое утверждение всё равно помним).
        # Обе записи обязаны коммититься вместе: иначе падение между add/invalidate
        # оставляет два активных взаимоисключающих факта.
        async with write_transaction() as conn:
            cur = await conn.execute(
                "SELECT id FROM user_memory WHERE id = ? AND user_id = ? "
                "AND valid_until IS NULL AND pinned = 0",
                (int(target), user_id),
            )
            active_target = await cur.fetchone()
            new_id = await _add_memory_in_transaction(
                conn,
                user_id,
                text,
                kind=kind,
                source_session_id=source_session_id,
            )
            if active_target is None:
                # Конкурентный прогон уже изменил выбранный target. Новый факт
                # остаётся обычным ADD; не инвалидируем чужую свежую revision.
                return {"action": "add", "id": new_id}
            invalidated = await _invalidate_memory_in_transaction(
                conn,
                user_id,
                int(target),
                superseded_by=new_id,
            )
            if not invalidated:  # pragma: no cover — BEGIN IMMEDIATE + check above
                raise RuntimeError("memory target became inactive inside transaction")
        log.info("user_memory.reconcile", action=action, target=target, new_id=new_id)
        return {"action": action, "id": new_id, "invalidated": int(target)}
    new_id = await add_memory(user_id, text, kind=kind, source_session_id=source_session_id)
    return {"action": "add", "id": new_id}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Жаккар ключевых токенов в [0,1]; 0 при пустом объединении."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    union = len(a | b)
    return inter / union if union else 0.0


def _cluster_by_jaccard(
    items: list[dict[str, Any]], threshold: float = 0.5
) -> list[list[dict[str, Any]]]:
    """Агломеративная кластеризация фактов по Жаккару ключевых токенов.

    Транзитивное замыкание (union-find): если a~b и b~c, то a,b,c — один кластер.
    ``threshold`` консервативный (0.5) — сливаем только явные дубли.
    """
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    toks = [_key_tokens(it["text"]) for it in items]
    for i in range(n):
        if not toks[i]:
            continue
        for j in range(i + 1, n):
            if toks[j] and _jaccard(toks[i], toks[j]) >= threshold:
                union(i, j)

    groups: dict[int, list[dict[str, Any]]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(items[i])
    return [g for g in groups.values() if len(g) >= 2]


def _pick_representative(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Самый полный/свежий факт кластера: длиннее текст, затем больший id (свежее)."""
    return max(
        group,
        key=lambda r: (len(r.get("text") or ""), int(r.get("id") or 0)),
    )


async def consolidate_memories(user_id: int, threshold: float = 0.6) -> list[dict[str, Any]]:
    """Слить дубли среди АКТУАЛЬНЫХ фактов пользователя (ночная Phase 3b).

    Группируем актуальные факты агломеративно по Жаккару ключевых токенов
    (``>= threshold``, по умолчанию 0.6 — выше прежних 0.5: короткие факты с
    1-2 общими токенами больше НЕ сливаются ложно, нужно реальное пересечение);
    в каждом кластере оставляем самый полный/свежий («representative»), остальные
    soft-invalidate через существующий механизм (``invalidate_memory`` →
    ``valid_until=now`` + ``superseded_by=rep_id``). Не создаём новых строк:
    representative УЖЕ актуален, индекс/эмбеддинг не трогаем повторно.
    Best-effort, идемпотентно (на чистой памяти — no-op).

    Возвращает список слияний: ``[{rep_id, rep_text, merged_ids:[…]}]`` —
    вызывающий (``reflection.run_dream_cycle``) логирует и реиндексирует.
    """
    rows = await list_memory(user_id, limit=1000)
    if len(rows) < 2:
        return []
    merges: list[dict[str, Any]] = []
    for group in _cluster_by_jaccard(rows, threshold):
        if any(bool(row.get("pinned")) for row in group):
            # Pinned memory is an explicit owner decision. Heuristic nightly
            # consolidation must never retire it or silently choose another
            # representative; the whole ambiguous cluster waits for review.
            log.info(
                "user_memory.consolidate_skipped_pinned",
                user_id=user_id,
                ids=[int(row["id"]) for row in group],
            )
            continue
        rep = _pick_representative(group)
        rep_id = int(rep["id"])
        merged_ids: list[int] = []
        for r in group:
            rid = int(r["id"])
            if rid == rep_id:
                continue
            try:
                ok = await invalidate_memory(user_id, rid, superseded_by=rep_id)
            except Exception as exc:  # noqa: BLE001 — слияние одного факта не валит цикл
                log.debug("user_memory.consolidate_invalidate_failed", id=rid, error=str(exc))
                continue
            if ok:
                merged_ids.append(rid)
            else:
                # invalidate вернул False: факт уже не актуален (гонка/повторный
                # прогон) или не наш — не сливаем, но фиксируем для диагностики.
                log.warning(
                    "user_memory.consolidate_invalidate_incomplete",
                    user_id=user_id,
                    id=rid,
                    rep_id=rep_id,
                )
        if merged_ids:
            log.info(
                "user_memory.consolidated",
                user_id=user_id,
                rep_id=rep_id,
                merged=len(merged_ids),
            )
            merges.append(
                {"rep_id": rep_id, "rep_text": str(rep["text"]), "merged_ids": merged_ids}
            )
    return merges


# Схема извлечения фактов для GBNF (Ollama complete_json) — корень анти-CJK:
# format=schema физически отрезает мусорные/китайские токены и битый JSON.
_FACTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["fact", "preference", "person", "project", "reminder", "other"],
                    },
                },
                "required": ["text"],
            },
        }
    },
    "required": ["facts"],
}


async def _extract_facts(
    client: Any,
    system: str,
    user: str,
    *,
    raise_on_provider_error: bool = False,
) -> list[dict[str, str]]:
    """Вытащить факты: GBNF-схема для Ollama (надёжно), строковый парсер — fallback."""
    from app.llm.client import CompletionRequest  # noqa: PLC0415

    # GBNF-путь (только Ollama) — форсит валидный JSON по схеме.
    if hasattr(client, "complete_json"):
        try:
            out = await client.complete_json(
                CompletionRequest(
                    system=system
                    + " Верни JSON {facts:[{text,kind}]}; пустой массив если фактов нет.",
                    user=user,
                    max_tokens=300,
                    temperature=0.0,
                ),
                _FACTS_SCHEMA,
            )
            res: list[dict[str, str]] = []
            for f in out.get("facts") or []:
                if isinstance(f, dict) and str(f.get("text", "")).strip():
                    res.append(
                        {"text": str(f["text"]).strip(), "kind": str(f.get("kind") or "fact")}
                    )
            return res
        except Exception as exc:  # noqa: BLE001 — падаем на строковый парсер
            log.debug("user_memory.extract_json_failed", error=str(exc))
    # Строковый fallback (облачные провайдеры / сбой схемы).
    try:
        out_text = await client.complete(
            CompletionRequest(system=system, user=user, max_tokens=200, temperature=0.1)
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("user_memory.extract_failed", error=str(exc))
        if raise_on_provider_error:
            raise RuntimeError("fact extraction provider failed") from exc
        return []
    facts: list[dict[str, str]] = []
    for raw in (out_text or "").splitlines():
        line = raw.strip().lstrip("-•*").strip()
        if not line or line.upper() in ("НЕТ", "NONE") or len(line) < 6:
            continue
        facts.append({"text": line, "kind": "fact"})
    return facts


async def extract_and_store(
    user_id: int, user_msg: str, assistant_msg: str, session_id: int | None = None
) -> int:
    """mem0-стиль: после обмена вытащить новые ДОЛГОВРЕМЕННЫЕ факты о пользователе
    и сохранить. Best-effort, дёшево (1 короткий LLM-вызов), не для каждого сообщения
    (вызывающий гейтит). Возвращает число добавленных фактов.
    """
    user_msg = (user_msg or "").strip()
    if len(user_msg) < 8:
        return 0
    from app.llm.client import LLMNotConfigured, make_client  # noqa: PLC0415

    # «Без тихого переполнения»: авто-рост памяти ограничен бюджетом.
    if await count_memory(user_id) >= MEMORY_AUTO_CAP:
        log.warning(
            "user_memory.auto_cap_reached",
            user_id=user_id,
            cap=MEMORY_AUTO_CAP,
            msg=(
                f"Достигнут лимит авто-извлечения ({MEMORY_AUTO_CAP} слотов) — "
                "новый факт НЕ сохранён. Почисти память в /settings/memory или "
                "закрепи важное вручную."
            ),
        )
        return 0
    try:
        client = make_client(kind="chat_summary", user_id=user_id)
    except LLMNotConfigured:
        return 0
    existing = await list_memory(user_id, limit=60)
    known = "\n".join("- " + e["text"] for e in existing) or "(пусто)"
    system = (
        "Ты ведёшь долговременную память личного ассистента. Из последнего обмена "
        "выпиши ТОЛЬКО новые, СТАБИЛЬНЫЕ факты о пользователе (имя/кто он, "
        "предпочтения, проекты, важные люди, постоянные задачи, цели, важные детали "
        "жизни). НЕ включай: сиюминутное, вопросы, общие знания и то, что уже известно. "
        "Кратко, от 3-го лица. Максимум 3 факта."
    )
    user = (
        f"Уже известно:\n{known}\n\nПоследний обмен:\n"
        f"Пользователь: {user_msg[:1500]}\nАссистент: {(assistant_msg or '')[:1200]}\n\n"
        "Новые факты (каждый с новой строки, начиная с «- »):"
    )
    facts = await _extract_facts(client, system, user)
    added = 0
    for f in facts:
        # mem0-стиль: разрешаем противоречия (update/delete) вместо тупого ADD,
        # чтобы новые факты не сосуществовали со старыми устаревшими.
        res = await reconcile_and_add(
            user_id, f["text"], kind=f.get("kind", "fact"), source_session_id=session_id
        )
        if res.get("action") in ("add", "update"):
            added += 1
        if added >= 3:
            break
    if added:
        log.info("user_memory.auto_added", user_id=user_id, count=added)
    return added


async def build_memory_block(user_id: int, max_items: int = 14) -> str:
    """Блок для системного промпта: закреплённые + недавние факты о пользователе,
    плюс свежие наблюдения ночной рефлексии («сон») отдельным коротким блоком."""
    blocks: list[str] = []
    # Ф3-C: при переполнении бюджета берём ВАЖНЫЕ факты (salience), не просто свежие.
    items = await list_memory(user_id, limit=max_items, order_by_salience=True)
    if items:
        lines = ["── Что я помню о тебе (личная память) ──"]
        for it in items:
            mark = "📌 " if it["pinned"] else "• "
            lines.append(f"{mark}{it['text']}")
        blocks.append("\n".join(lines))
    # Выводы ночной рефлексии (insight/dream) — отдельным блоком, только если есть
    # актуальные. Best-effort: на старой БД без таблицы reflection тихо пропускаем.
    try:
        from app.dreams import list_active_reflections  # noqa: PLC0415

        refl = await list_active_reflections(user_id, kinds=["insight", "dream"], limit=4)
    except Exception as exc:  # noqa: BLE001 — рефлексии опциональны, не ломаем промпт
        log.debug("user_memory.reflections_skip", error=str(exc))
        refl = []
    if refl:
        rlines = ["── Что я заметил о тебе ──"]
        for r in refl:
            rlines.append(f"• {r['text']}")
        blocks.append("\n".join(rlines))
    return "\n\n".join(blocks)
