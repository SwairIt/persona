"""Ночная рефлексия («сон») — leased proposal-only цикл памяти.

docs/MEMORY_RESEARCH.md §3. Запускается ночным воркером
(``app/workers/dream_worker.py``) раз в сутки (opt-in kv ``dream_enabled``).
Три фазы:

  1. **Light Sleep** — собрать «жизнь недели» (чат + OCR экрана + расшифровка
     речи) за ``dream_lookback_days``; по одному Ollama-вызову на источник
     вытащить durable-факты (``_FACTS_SCHEMA`` из ``user_memory``); сдедупить в
     кандидаты со счётчиками частоты / числа сессий / свежести / богатства
     (БЕЗ записи в постоянную память).
  2. **REM** — один Ollama-вызов «повторяющиеся темы недели» → нарратив в
     ``reflection`` (``kind='dream'``), недеструктивно.
  3. **Deep Sleep** — взвешенный скоринг каждого кандидата (Hermes-формула §3.2:
     relevance·0.30 + frequency·0.24 + diversity·0.15 + recency·0.15 +
     consolidation·0.10 + richness·0.06). Сначала все кандидаты и evidence
     фиксируются в durable ledger. Затем отдельная детерминированная policy
     допускает только доказанные owner-факты; LLM никогда не исполняет изменение
     памяти напрямую.

Run имеет idempotency key, lease, retry и append-only audit/revision. Ошибка
извлечения оставляет input cursor на месте; cursor и завершение run коммитятся
атомарно. Гейт активности переносит прогон, если владелец недавно писал.

kv-настройки (все опц., с дефолтами):
    dream_enabled='0' (opt-in), dream_hour_local=3, dream_lookback_days=7,
    dream_max_candidates=50, dream_promotion_threshold=0.6,
    dream_min_recall_count=2.
Маркеры: dream_last_processed_message_id (анти-двойной-промоут),
    dream_last_fired (per-date, ставит ClockScheduler).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.domains.memory.dream import DreamCandidate

log = get_logger("persona.reflection")

# Дефолты Hermes (docs §1.4 / §3.1).
_DEF_LOOKBACK_DAYS = 7
_DEF_MAX_CANDIDATES = 50
_DEF_PROMOTION_THRESHOLD = 0.6
_DEF_MIN_RECALL_COUNT = 2
_QUIET_MINUTES = 60  # гейт активности: не «спим», пока пользователь не угомонился

# Анти-перегруз Ollama ночью: сколько «документов» (сессий/дней) максимум
# прогоняем через извлечение фактов за один цикл. Каждый документ — 1 LLM-вызов.
_MAX_DOCS = 24
_KV_LAST_MSG_ID = "dream_last_processed_message_id"
_TELEGRAM_GROUP_PREFIX = "[Telegram · "

_SYS_LIGHT = (
    "Ты ведёшь долговременную память личного ассистента. Из транскрипта за день "
    "(чат + что было на экране + расшифровка речи) выпиши ТОЛЬКО durable-факты/"
    "предпочтения/важных людей/проекты/цели. НЕ включай: сиюминутное, вопросы, "
    "общие знания. Кратко, от 3-го лица."
)
_SYS_REM = (
    "Подведи итог: какие повторяющиеся темы и паттерны видны в этих заметках за "
    "неделю? 2–4 коротких наблюдения о пользователе, от 3-го лица, без воды."
)


# ── kv-хелперы (тихий fallback на дефолт) ────────────────────────────────────


async def _kv_int(key: str, default: int) -> int:
    try:
        async with get_connection() as conn:
            raw = await get_kv(conn, key)
        if raw is None:
            return default
        return int(str(raw).strip())
    except Exception:  # noqa: BLE001 — битое значение / нет БД → дефолт
        return default


async def _kv_float(key: str, default: float) -> float:
    try:
        async with get_connection() as conn:
            raw = await get_kv(conn, key)
        if raw is None:
            return default
        return float(str(raw).strip())
    except Exception:  # noqa: BLE001 — битое значение / нет БД → дефолт
        return default


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_sqlite_dt(value: str | None) -> datetime | None:
    """Распарсить ``datetime('now')``-штамп SQLite (UTC, без tz) в aware-UTC."""
    if not value:
        return None
    raw = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[: len(fmt) + 6], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _norm_key(text: str) -> str:
    return " ".join((text or "").casefold().split())


def _key_tokens(text: str) -> set[str]:
    return {w for w in _norm_key(text).split() if len(w) >= 4}


def _is_untrusted_group_message(text: str) -> bool:
    """Return whether a stored chat row came from a Telegram group participant.

    ``PersonaTelegramService`` currently prefixes every group message (including
    passive observations) with ``[Telegram · <speaker>]`` while owner DMs are
    stored verbatim.  Until chat messages have durable origin/speaker columns,
    fail closed: group speech must not be promoted into facts about the owner.

    TODO(agent-provenance): replace this compatibility marker with explicit
    ``origin_surface`` / ``origin_actor`` fields and a source trust policy.
    """

    return (text or "").lstrip().startswith(_TELEGRAM_GROUP_PREFIX)


def _safe_processed_cursor(
    last_msg_id: int,
    scanned_message_ids: set[int],
    ignored_message_ids: set[int],
    processed_message_ids: set[int],
) -> int:
    """Largest safe cursor that does not pass an unprocessed owner message.

    Ignored ids are deterministically ineligible (currently short noise and
    Telegram group speech).  Every other scanned id must have reached a
    successful extraction call before the cursor may move past it.
    """

    safe_ids = ignored_message_ids | processed_message_ids
    pending_ids = scanned_message_ids - safe_ids
    if pending_ids:
        return max(int(last_msg_id), min(pending_ids) - 1)
    if not scanned_message_ids:
        return int(last_msg_id)
    return max(int(last_msg_id), *scanned_message_ids)


# ── гейт активности ──────────────────────────────────────────────────────────


async def _is_quiet(now: datetime) -> bool:
    """True, если последняя реплика чата старше ``_QUIET_MINUTES`` (можно «спать»)."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute("SELECT MAX(created_at) AS mx FROM chat_message")
            row = await cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — нет таблицы → считаем тихо
        log.debug("reflection.quiet_check_failed", error=str(exc))
        return True
    last = _parse_sqlite_dt(row["mx"] if row else None)
    if last is None:
        return True
    return (now - last) >= timedelta(minutes=_QUIET_MINUTES)


# ── Phase 1: Light Sleep — собрать документы и извлечь кандидатов ─────────────


async def _gather_documents(
    user_id: int, cutoff: str, last_msg_id: int
) -> tuple[list[dict[str, Any]], set[int], set[int]]:
    """Собрать «документы» недели (чат по сессиям + OCR/аудио по дням).

    Возвращает ``(documents, scanned_message_ids, ignored_message_ids)``.
    Каждый документ:
    ``{source, text, message_ids, latest_at}``. ``source`` — ключ для
    diversity-метрики (``chat:<sid>`` / ``screen:<day>`` / ``audio:<day>``).

    ``message_ids`` содержит только реплики, реально вошедшие в 6000-символьный
    документ. Остальные остаются pending и не могут быть перескочены курсором.
    """
    docs: list[dict[str, Any]] = []
    scanned_message_ids: set[int] = set()
    ignored_message_ids: set[int] = set()

    # 1) Чат: реплики пользователя за окно, новее last_msg_id, по сессиям.
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT m.id, m.content, m.session_id, m.created_at "
                "FROM chat_message m JOIN chat_session s ON s.id = m.session_id "
                "WHERE s.user_id = ? AND m.role = 'user' "
                "AND m.created_at >= ? AND m.id > ? "
                "ORDER BY m.session_id, m.id",
                (user_id, cutoff, last_msg_id),
            )
            rows = await cur.fetchall()
        by_session: dict[int, dict[str, Any]] = {}
        for r in rows:
            mid = int(r["id"])
            scanned_message_ids.add(mid)
            raw_content = str(r["content"] or "")
            if _is_untrusted_group_message(raw_content):
                ignored_message_ids.add(mid)
                continue
            sid = int(r["session_id"])
            slot = by_session.setdefault(sid, {"items": [], "latest": r["created_at"]})
            txt = " ".join(raw_content.split())
            if len(txt) >= 4:
                slot["items"].append((mid, txt[:600], r["created_at"]))
                slot["latest"] = r["created_at"]
            else:
                ignored_message_ids.add(mid)
        for sid, slot in by_session.items():
            if not slot["items"]:
                continue
            texts: list[str] = []
            included_ids: list[int] = []
            used_chars = 0
            for mid, text, created_at in slot["items"]:
                added_chars = len(text) + (1 if texts else 0)
                if used_chars + added_chars > 6000:
                    break
                texts.append(text)
                included_ids.append(int(mid))
                used_chars += added_chars
                slot["latest"] = created_at
            docs.append(
                {
                    "source": f"chat:{sid}",
                    "text": "\n".join(texts),
                    "message_ids": included_ids,
                    "latest_at": slot["latest"],
                }
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("reflection.gather_chat_failed", error=str(exc))

    # 2) Экран: OCR-текст за окно, сгруппирован по дню (таблица глобальная — без user_id).
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT ocr_text, captured_at FROM screenshots "
                "WHERE captured_at >= ? AND ocr_text IS NOT NULL "
                "AND length(ocr_text) >= 40 ORDER BY captured_at DESC LIMIT 300",
                (cutoff,),
            )
            rows = await cur.fetchall()
        by_day: dict[str, dict[str, Any]] = {}
        for r in rows:
            day = str(r["captured_at"])[:10]
            slot = by_day.setdefault(day, {"texts": [], "latest": r["captured_at"]})
            if len(slot["texts"]) < 12:
                slot["texts"].append(" ".join((r["ocr_text"] or "").split())[:500])
        for day, slot in by_day.items():
            docs.append(
                {
                    "source": f"screen:{day}",
                    "text": "\n".join(slot["texts"])[:6000],
                    "message_ids": [],
                    "latest_at": slot["latest"],
                }
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("reflection.gather_screen_failed", error=str(exc))

    # 3) Аудио: расшифровки речи за окно, по дням.
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT transcript, started_at FROM audio_segment "
                "WHERE started_at >= ? AND transcript IS NOT NULL "
                "AND length(transcript) >= 20 ORDER BY started_at DESC LIMIT 300",
                (cutoff,),
            )
            rows = await cur.fetchall()
        by_day_a: dict[str, dict[str, Any]] = {}
        for r in rows:
            day = str(r["started_at"])[:10]
            slot = by_day_a.setdefault(day, {"texts": [], "latest": r["started_at"]})
            if len(slot["texts"]) < 20:
                slot["texts"].append(" ".join((r["transcript"] or "").split())[:500])
        for day, slot in by_day_a.items():
            docs.append(
                {
                    "source": f"audio:{day}",
                    "text": "\n".join(slot["texts"])[:6000],
                    "message_ids": [],
                    "latest_at": slot["latest"],
                }
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("reflection.gather_audio_failed", error=str(exc))

    # Сначала старейшие pending chat-документы: иначе лимит _MAX_DOCS каждый
    # прогон выбирал бы одни и те же новые сессии, а старые навсегда блокировали
    # безопасное продвижение cursor. Не-chat документы идут следом, свежие первыми.
    chat_docs = [doc for doc in docs if doc.get("message_ids")]
    other_docs = [doc for doc in docs if not doc.get("message_ids")]
    chat_docs.sort(key=lambda doc: min(doc["message_ids"]))
    other_docs.sort(key=lambda doc: str(doc.get("latest_at") or ""), reverse=True)
    return (
        (chat_docs + other_docs)[:_MAX_DOCS],
        scanned_message_ids,
        ignored_message_ids,
    )


async def _light_sleep_detailed(
    client: Any,
    docs: list[dict[str, Any]],
    max_candidates: int,
    *,
    on_progress: Callable[[], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], set[int], list[str]]:
    """Извлечь durable-факты из документов, сдедупить в кандидаты со счётчиками.

    Кандидат: ``{text, kind, count, sources:set, message_ids:list, latest_at,
    richness}``. Один Ollama-вызов на документ (``_extract_facts`` + GBNF).
    Второй элемент результата — ids chat-реплик, чей документ успешно прошёл
    extraction (включая валидный пустой результат).
    """
    from app.chat.user_memory import _extract_facts  # noqa: PLC0415

    cands: dict[str, dict[str, Any]] = {}
    processed_message_ids: set[int] = set()
    failed_sources: list[str] = []
    for doc in docs:
        if len(cands) >= max_candidates:
            break
        try:
            facts = await _extract_facts(
                client,
                _SYS_LIGHT,
                f"Заметки источника:\n{doc['text']}",
                raise_on_provider_error=True,
            )
        except Exception as exc:  # noqa: BLE001 — Ollama лёг → этот документ пропускаем
            log.debug("reflection.extract_failed", source=doc["source"], error=str(exc))
            failed_sources.append(str(doc["source"]))
            continue
        if on_progress is not None:
            await on_progress()
        processed_message_ids.update(int(mid) for mid in (doc.get("message_ids") or []))
        for f in facts:
            text = " ".join(str(f.get("text") or "").split())
            if len(text) < 6:
                continue
            key = _norm_key(text)
            slot = cands.get(key)
            if slot is None:
                if len(cands) >= max_candidates:
                    continue
                slot = cands[key] = {
                    "text": text,
                    "kind": str(f.get("kind") or "fact"),
                    "count": 0,
                    "sources": set(),
                    "message_ids": [],
                    "latest_at": doc.get("latest_at"),
                    "richness": len(text),
                    "evidence": [],
                }
            source = str(doc["source"])
            source_kind = (
                "owner_chat"
                if source.startswith("chat:")
                else "screen"
                if source.startswith("screen:")
                else "audio"
                if source.startswith("audio:")
                else "other"
            )
            content_hash = hashlib.sha256(str(doc["text"]).encode("utf-8")).hexdigest()
            message_ids = [int(mid) for mid in (doc.get("message_ids") or [])]
            evidence_ids: list[int | None] = message_ids or [None]
            for message_id in evidence_ids:
                slot["evidence"].append(
                    {
                        "source_kind": source_kind,
                        "source_ref": source,
                        "source_message_id": message_id,
                        "owner_attributed": source_kind == "owner_chat",
                        "content_hash": content_hash,
                        "excerpt": str(doc["text"])[:500],
                        "observed_at": doc.get("latest_at"),
                    }
                )
            slot["count"] += 1
            slot["sources"].add(doc["source"])
            slot["message_ids"].extend(doc.get("message_ids") or [])
            if str(doc.get("latest_at") or "") > str(slot.get("latest_at") or ""):
                slot["latest_at"] = doc.get("latest_at")
    # Кластеризация близких кандидатов (агломеративно по Jaccard ключевых токенов
    # ≥ 0.6): дубли-перефразировки сливаем в один representative (самый свежий/
    # богатый), суммируя счётчики/источники — снижает дубли ДО скоринга/промоута.
    return _cluster_candidates(list(cands.values())), processed_message_ids, failed_sources


async def _light_sleep(
    client: Any, docs: list[dict[str, Any]], max_candidates: int
) -> tuple[list[dict[str, Any]], set[int]]:
    """Compatibility wrapper used by focused extraction tests."""
    candidates, processed, _failed = await _light_sleep_detailed(
        client, docs, max_candidates
    )
    return candidates, processed


def _cluster_candidates(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Слить близкие кандидаты (Jaccard ключевых токенов ≥ 0.6) в representative.

    Агломеративно (транзитивное замыкание union-find): a~b, b~c → один кластер.
    Representative = самый свежий/богатый; count/sources/message_ids/richness
    объединяются по кластеру (важное всплывает, дубли схлопываются).

    Порог 0.6 (был 0.5) консистентен с ``user_memory.consolidate_memories`` и
    ``_is_consolidated``: меньше ложных слияний коротких перефразировок.
    """
    n = len(cands)
    if n < 2:
        return cands
    toks = [_key_tokens(c["text"]) for c in cands]
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        if not toks[i]:
            continue
        ti = toks[i]
        for j in range(i + 1, n):
            if not toks[j]:
                continue
            inter = len(ti & toks[j])
            if not inter:
                continue
            union = len(ti | toks[j])
            if union and inter / union >= 0.6:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    merged: list[dict[str, Any]] = []
    for idxs in groups.values():
        if len(idxs) == 1:
            merged.append(cands[idxs[0]])
            continue
        # Representative: свежее (latest_at), при равенстве — богаче (richness).
        rep_i = max(
            idxs,
            key=lambda k: (
                str(cands[k].get("latest_at") or ""),
                int(cands[k].get("richness") or 0),
            ),
        )
        rep = dict(cands[rep_i])
        rep["sources"] = set(rep.get("sources") or set())
        rep["message_ids"] = list(rep.get("message_ids") or [])
        rep["evidence"] = list(rep.get("evidence") or [])
        for k in idxs:
            if k == rep_i:
                continue
            other = cands[k]
            rep["count"] = rep.get("count", 0) + other.get("count", 0)
            rep["sources"].update(other.get("sources") or set())
            rep["message_ids"].extend(other.get("message_ids") or [])
            rep["evidence"].extend(other.get("evidence") or [])
            rep["richness"] = max(rep.get("richness", 0), other.get("richness", 0))
        merged.append(rep)
    return merged


# ── Phase 3: Deep Sleep — скоринг + промоут ──────────────────────────────────


def _centrality(cands: list[dict[str, Any]]) -> dict[int, float]:
    """relevance-прокси БЕЗ LLM: насколько кандидат «в теме» недели — доля других
    кандидатов, разделяющих с ним ключевой токен. В [0,1]."""
    toks = [(_key_tokens(c["text"])) for c in cands]
    n = len(cands)
    out: dict[int, float] = {}
    for i in range(n):
        if n <= 1 or not toks[i]:
            out[i] = 0.5  # одиночка / без ключевых токенов → нейтрально
            continue
        shared = sum(1 for j in range(n) if j != i and (toks[i] & toks[j]))
        out[i] = shared / (n - 1)
    return out


async def _already_known(user_id: int) -> list[set[str]]:
    """Ключевые токены актуальных фактов user_memory (для consolidation-штрафа)."""
    try:
        from app.chat.user_memory import list_memory  # noqa: PLC0415

        rows = await list_memory(user_id, limit=500)
    except Exception as exc:  # noqa: BLE001
        log.debug("reflection.known_failed", error=str(exc))
        return []
    return [_key_tokens(r["text"]) for r in rows]


def _is_consolidated(cand_tokens: set[str], known: list[set[str]]) -> bool:
    """True, если факт уже по сути в памяти (Jaccard ключевых токенов ≥ 0.6)."""
    if not cand_tokens:
        return False
    for kt in known:
        if not kt:
            continue
        inter = len(cand_tokens & kt)
        if inter == 0:
            continue
        union = len(cand_tokens | kt)
        if union and inter / union >= 0.6:
            return True
    return False


def _score(cand: dict[str, Any], relevance: float, consolidated: bool, now: datetime) -> float:
    """Hermes взвешенная сумма (docs §3.2), компоненты уже в [0,1]."""
    latest = _parse_sqlite_dt(cand.get("latest_at"))
    hours = max(0.0, (now - latest).total_seconds() / 3600.0) if latest else 168.0
    recency = 0.995**hours
    frequency = min(1.0, cand["count"] / 3.0)
    diversity = min(1.0, len(cand["sources"]) / 3.0)
    richness = min(1.0, cand["richness"] / 200.0)
    consolidation = 0.0 if consolidated else 1.0
    return (
        0.30 * relevance
        + 0.24 * frequency
        + 0.15 * diversity
        + 0.15 * recency
        + 0.10 * consolidation
        + 0.06 * richness
    )


# ── Оркестратор цикла ────────────────────────────────────────────────────────


async def run_dream_cycle(user_id: int) -> dict[str, Any]:
    """Run a leased, idempotent, proposal-only nightly memory cycle."""
    from app.adapters.memory.dream_repository import SqliteDreamLedger  # noqa: PLC0415
    from app.application.memory.dream_service import DreamLedgerService  # noqa: PLC0415
    from app.application.memory.ports import (  # noqa: PLC0415
        DreamApplySummary,
        DreamCompletionReport,
    )
    from app.domains.memory.dream import DreamPolicy  # noqa: PLC0415

    now = _utc_now()
    if not await _is_quiet(now):
        log.info("reflection.quiet_gate", user_id=user_id)
        return {"status": "quiet"}

    lookback = max(1, await _kv_int("dream_lookback_days", _DEF_LOOKBACK_DAYS))
    max_candidates = max(1, await _kv_int("dream_max_candidates", _DEF_MAX_CANDIDATES))
    threshold = await _kv_float("dream_promotion_threshold", _DEF_PROMOTION_THRESHOLD)
    min_recall = max(1, await _kv_int("dream_min_recall_count", _DEF_MIN_RECALL_COUNT))
    last_msg_id = await _kv_int(_KV_LAST_MSG_ID, 0)
    config: dict[str, object] = {
        "lookback_days": lookback,
        "max_candidates": max_candidates,
        "promotion_threshold": threshold,
        "min_recall_count": min_recall,
        "pipeline": "proposal-policy-v1",
    }
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    idempotency_key = (
        f"dream:{user_id}:{now.strftime('%Y-%m-%d')}:{last_msg_id}:{config_hash}"
    )
    worker_id = f"dream-{uuid.uuid4().hex}"
    ledger = SqliteDreamLedger()
    lease = await ledger.acquire_run(
        user_id=user_id,
        idempotency_key=idempotency_key,
        worker_id=worker_id,
        input_cursor=last_msg_id,
        config=config,
        lease_seconds=1800,
    )
    if not lease.acquired:
        status = "duplicate" if lease.status == "completed" else "retry"
        log.info(
            "reflection.run_not_acquired",
            user_id=user_id,
            run_id=lease.run_id,
            run_status=lease.status,
        )
        return {
            "status": status,
            "candidates": 0,
            "promoted": 0,
            "dream": False,
            "run_id": lease.run_id,
        }

    cutoff = (now - timedelta(days=lookback)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        docs, scanned_message_ids, ignored_message_ids = await _gather_documents(
            user_id, cutoff, last_msg_id
        )
        if not docs:
            safe_cursor = _safe_processed_cursor(
                last_msg_id, scanned_message_ids, ignored_message_ids, set()
            )
            empty = DreamApplySummary(candidates=0, applied=0, rejected=0, noops=0)
            await ledger.complete_run(
                lease,
                safe_cursor=safe_cursor,
                summary=empty,
                report=DreamCompletionReport(),
            )
            log.info("reflection.no_documents", user_id=user_id, run_id=lease.run_id)
            return {
                "status": "no_data",
                "candidates": 0,
                "promoted": 0,
                "dream": False,
                "run_id": lease.run_id,
            }

        try:
            from app.llm.client import make_client  # noqa: PLC0415

            client = make_client(kind="chat_summary")
        except Exception as exc:  # noqa: BLE001
            await ledger.retry_run(
                lease,
                error=f"LLM unavailable: {exc}",
                retry_seconds=300,
                safe_cursor=last_msg_id,
            )
            log.info("reflection.no_llm", user_id=user_id, error=str(exc))
            return {
                "status": "retry",
                "candidates": 0,
                "promoted": 0,
                "dream": False,
                "run_id": lease.run_id,
            }

        async def heartbeat() -> None:
            await ledger.heartbeat(lease, lease_seconds=1800)

        cands, processed_message_ids, failed_sources = await _light_sleep_detailed(
            client,
            docs,
            max_candidates,
            on_progress=heartbeat,
        )
        safe_cursor = _safe_processed_cursor(
            last_msg_id,
            scanned_message_ids,
            ignored_message_ids,
            processed_message_ids,
        )
        proposals = await _build_proposals(user_id, cands, now)
        service = DreamLedgerService(ledger)
        policy = DreamPolicy(
            score_threshold=threshold,
            min_recall_count=min_recall,
            memory_cap=80,
        )
        summary = await service.apply_proposals(lease, proposals, policy)

        # A failed extraction keeps the whole input cursor retryable.  Already
        # applied candidates are terminal/idempotent inside this same run.
        if failed_sources:
            await ledger.retry_run(
                lease,
                error="extraction failed for: " + ", ".join(failed_sources[:12]),
                retry_seconds=300,
                safe_cursor=last_msg_id,
            )
            return {
                "status": "retry",
                "candidates": summary.candidates,
                "promoted": summary.applied,
                "dream": False,
                "run_id": lease.run_id,
            }

        dream_text = await _rem(client, cands) if cands else ""
        dream_written = bool(dream_text)
        impact_score = round(summary.applied / max(1, summary.candidates), 4)
        source_ids = tuple(
            int(message_id)
            for cand in cands
            for message_id in (cand.get("message_ids") or [])
        )
        # Report, REM reflection, cursor, run state, and completion audit are
        # one commit.  A failure leaves the run retryable with the old cursor.
        await ledger.complete_run(
            lease,
            safe_cursor=safe_cursor,
            summary=summary,
            report=DreamCompletionReport(
                dream_text=dream_text,
                source_message_ids=source_ids,
                impact_score=impact_score,
            ),
        )
        log.info(
            "reflection.cycle_done",
            user_id=user_id,
            run_id=lease.run_id,
            docs=len(docs),
            candidates=summary.candidates,
            promoted=summary.applied,
            rejected=summary.rejected,
            dream=dream_written,
            impact=impact_score,
        )
        return {
            "status": "ok" if cands else "no_data",
            "candidates": summary.candidates,
            "promoted": summary.applied,
            "rejected": summary.rejected,
            "noops": summary.noops,
            "consolidations": 0,
            "conflicts": 0,
            "dream": dream_written,
            "impact_score": impact_score,
            "run_id": lease.run_id,
        }
    except asyncio.CancelledError:
        await asyncio.shield(
            ledger.retry_run(
                lease,
                error="dream cycle cancelled",
                retry_seconds=60,
                safe_cursor=last_msg_id,
            )
        )
        raise
    except Exception as exc:  # noqa: BLE001
        try:
            await ledger.retry_run(
                lease,
                error=f"{type(exc).__name__}: {exc}",
                retry_seconds=300,
                safe_cursor=last_msg_id,
            )
        except Exception as retry_exc:  # noqa: BLE001
            log.error(
                "reflection.retry_ledger_failed",
                run_id=lease.run_id,
                error=str(retry_exc),
            )
        log.exception("reflection.cycle_failed", user_id=user_id, run_id=lease.run_id)
        return {
            "status": "retry",
            "candidates": 0,
            "promoted": 0,
            "dream": False,
            "run_id": lease.run_id,
        }


async def _build_proposals(
    user_id: int,
    cands: list[dict[str, Any]],
    now: datetime,
) -> tuple[DreamCandidate, ...]:
    """Turn generated candidates into immutable domain proposals."""
    from app.domains.memory.dream import DreamCandidate, DreamEvidence  # noqa: PLC0415

    relevance = _centrality(cands)
    proposals: list[DreamCandidate] = []
    # Consolidation affects scoring only.  It does not mutate existing memory.
    known = await _already_known(user_id)
    for index, cand in enumerate(cands):
        score = _score(
            cand,
            relevance.get(index, 0.5),
            _is_consolidated(_key_tokens(cand["text"]), known),
            now,
        )
        evidence = tuple(
            DreamEvidence(
                source_kind=str(item["source_kind"]),
                source_ref=str(item["source_ref"]),
                source_message_id=item.get("source_message_id"),
                owner_attributed=bool(item.get("owner_attributed")),
                content_hash=str(item["content_hash"]),
                excerpt=str(item["excerpt"]) if item.get("excerpt") else None,
                observed_at=(
                    str(item["observed_at"]) if item.get("observed_at") is not None else None
                ),
            )
            for item in cand.get("evidence", [])
        )
        key_material = (
            f"{str(cand['kind']).casefold()}|"
            f"{_norm_key(str(cand['text']))}"
        )
        proposals.append(
            DreamCandidate(
                key=hashlib.sha256(key_material.encode("utf-8")).hexdigest(),
                text=str(cand["text"]),
                kind=str(cand["kind"]),
                proposed_action="add",
                score=score,
                observed_count=max(1, int(cand["count"])),
                source_count=max(1, len(cand["sources"])),
                evidence=evidence,
            )
        )
    return tuple(proposals)


async def _rem(client: Any, cands: list[dict[str, Any]]) -> str:
    """Generate a REM narrative without writing it.

    The repository stores it together with the report/cursor/run completion.
    """
    from app.llm.client import CompletionRequest  # noqa: PLC0415

    top = sorted(cands, key=lambda c: -c["count"])[:20]
    notes = "\n".join(f"- {c['text']}" for c in top)
    try:
        narrative = await client.complete(
            CompletionRequest(
                system=_SYS_REM,
                user=f"Заметки за неделю:\n{notes}",
                max_tokens=300,
                temperature=0.3,
            )
        )
    except Exception as exc:  # noqa: BLE001 — Ollama лёг → REM просто пропускаем
        log.debug("reflection.rem_failed", error=str(exc))
        return ""
    narrative = (narrative or "").strip()
    if len(narrative) < 12:
        return ""
    return narrative


__all__ = ["run_dream_cycle"]
