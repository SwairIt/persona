"""Ночная консолидация памяти («сон») — Hermes-style 3-фазный цикл рефлексии.

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
     consolidation·0.10 + richness·0.06); промоут кандидатов со
     ``score > dream_promotion_threshold`` И встреченных в
     ``>= dream_min_recall_count`` сессиях через ``reconcile_and_add`` (mem0
     ADD/UPDATE/DELETE/NOOP + bi-temporal); реиндекс источников.

Любой сбой Ollama (туннель часто лежит) → тихий degrade: фаза пропускается, цикл
НЕ падает (``log.debug`` + continue). Гейт активности (``quiet_minutes`` = 60)
переносит прогон на следующий тик, если пользователь активничал только что.

kv-настройки (все опц., с дефолтами):
    dream_enabled='0' (opt-in), dream_hour_local=3, dream_lookback_days=7,
    dream_max_candidates=50, dream_promotion_threshold=0.6,
    dream_min_recall_count=2.
Маркеры: dream_last_processed_message_id (анти-двойной-промоут),
    dream_last_fired (per-date, ставит ClockScheduler).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

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
) -> tuple[list[dict[str, Any]], int]:
    """Собрать «документы» недели (чат по сессиям + OCR/аудио по дням).

    Возвращает (documents, max_message_id). Каждый документ:
    ``{source, text, message_ids, latest_at}``. ``source`` — ключ для
    diversity-метрики (``chat:<sid>`` / ``screen:<day>`` / ``audio:<day>``).
    """
    docs: list[dict[str, Any]] = []
    max_msg_id = last_msg_id

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
            max_msg_id = max(max_msg_id, mid)
            sid = int(r["session_id"])
            slot = by_session.setdefault(
                sid, {"texts": [], "ids": [], "latest": r["created_at"]}
            )
            txt = " ".join((r["content"] or "").split())
            if len(txt) >= 4:
                slot["texts"].append(txt[:600])
                slot["ids"].append(mid)
                slot["latest"] = r["created_at"]
        for sid, slot in by_session.items():
            if not slot["texts"]:
                continue
            docs.append(
                {
                    "source": f"chat:{sid}",
                    "text": "\n".join(slot["texts"])[:6000],
                    "message_ids": slot["ids"][:20],
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

    # Свежие документы — первыми (важное в начало, less-is-more для 4–8B).
    docs.sort(key=lambda d: str(d.get("latest_at") or ""), reverse=True)
    return docs[:_MAX_DOCS], max_msg_id


async def _light_sleep(
    client: Any, docs: list[dict[str, Any]], max_candidates: int
) -> list[dict[str, Any]]:
    """Извлечь durable-факты из документов, сдедупить в кандидаты со счётчиками.

    Кандидат: ``{text, kind, count, sources:set, message_ids:list, latest_at,
    richness}``. Один Ollama-вызов на документ (``_extract_facts`` + GBNF).
    """
    from app.chat.user_memory import _extract_facts  # noqa: PLC0415

    cands: dict[str, dict[str, Any]] = {}
    for doc in docs:
        if len(cands) >= max_candidates:
            break
        try:
            facts = await _extract_facts(
                client, _SYS_LIGHT, f"Заметки источника:\n{doc['text']}"
            )
        except Exception as exc:  # noqa: BLE001 — Ollama лёг → этот документ пропускаем
            log.debug("reflection.extract_failed", source=doc["source"], error=str(exc))
            continue
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
                }
            slot["count"] += 1
            slot["sources"].add(doc["source"])
            slot["message_ids"].extend(doc.get("message_ids") or [])
            if str(doc.get("latest_at") or "") > str(slot.get("latest_at") or ""):
                slot["latest_at"] = doc.get("latest_at")
    # Кластеризация близких кандидатов (агломеративно по Jaccard ключевых токенов
    # ≥ 0.5): дубли-перефразировки сливаем в один representative (самый свежий/
    # богатый), суммируя счётчики/источники — снижает дубли ДО скоринга/промоута.
    return _cluster_candidates(list(cands.values()))


def _cluster_candidates(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Слить близкие кандидаты (Jaccard ключевых токенов ≥ 0.5) в representative.

    Агломеративно (транзитивное замыкание union-find): a~b, b~c → один кластер.
    Representative = самый свежий/богатый; count/sources/message_ids/richness
    объединяются по кластеру (важное всплывает, дубли схлопываются).
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
            if union and inter / union >= 0.5:
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
        for k in idxs:
            if k == rep_i:
                continue
            other = cands[k]
            rep["count"] = rep.get("count", 0) + other.get("count", 0)
            rep["sources"].update(other.get("sources") or set())
            rep["message_ids"].extend(other.get("message_ids") or [])
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


def _score(
    cand: dict[str, Any], relevance: float, consolidated: bool, now: datetime
) -> float:
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
    """Один ночной цикл «сна» для пользователя. Всегда best-effort.

    Возвращает статус-словарь:
      * ``{"status": "quiet"}``        — пользователь активничал, отложить;
      * ``{"status": "no_data", ...}`` — нет материала/LLM, цикл пуст;
      * ``{"status": "ok", "candidates", "promoted", "dream"}``.

    Никогда не поднимает наружу (кроме отмены): любой сбой логируется и
    превращается в безопасный статус, чтобы воркер не падал.
    """
    now = _utc_now()
    if not await _is_quiet(now):
        log.info("reflection.quiet_gate", user_id=user_id)
        return {"status": "quiet"}

    lookback = max(1, await _kv_int("dream_lookback_days", _DEF_LOOKBACK_DAYS))
    max_candidates = max(1, await _kv_int("dream_max_candidates", _DEF_MAX_CANDIDATES))
    threshold = await _kv_float("dream_promotion_threshold", _DEF_PROMOTION_THRESHOLD)
    min_recall = max(1, await _kv_int("dream_min_recall_count", _DEF_MIN_RECALL_COUNT))
    last_msg_id = await _kv_int(_KV_LAST_MSG_ID, 0)

    cutoff = (now - timedelta(days=lookback)).strftime("%Y-%m-%d %H:%M:%S")
    docs, max_msg_id = await _gather_documents(user_id, cutoff, last_msg_id)
    if not docs:
        log.info("reflection.no_documents", user_id=user_id)
        return {"status": "no_data", "candidates": 0, "promoted": 0, "dream": False}

    # LLM-клиент (туннель/Ollama). Нет клиента → нет извлечения → тихий no-op.
    try:
        from app.llm.client import make_client  # noqa: PLC0415

        client = make_client(kind="chat_summary")
    except Exception as exc:  # noqa: BLE001 — LLMNotConfigured и пр.
        log.info("reflection.no_llm", user_id=user_id, error=str(exc))
        return {"status": "no_data", "candidates": 0, "promoted": 0, "dream": False}

    # Phase 1 — Light Sleep.
    cands = await _light_sleep(client, docs, max_candidates)
    if not cands:
        # Помечаем обработанные сообщения, чтобы не гонять их повторно.
        await _set_last_processed(max_msg_id)
        log.info("reflection.no_candidates", user_id=user_id, docs=len(docs))
        return {"status": "no_data", "candidates": 0, "promoted": 0, "dream": False}

    # Phase 2 — REM (нарратив тем недели, недеструктивно).
    dream_text = await _rem(client, user_id, cands)
    dream_written = bool(dream_text)

    # Phase 3 — Deep Sleep (скоринг + промоут).
    deep = await _deep_sleep(user_id, cands, threshold, min_recall, now)
    promoted = deep["promoted"]
    conflicts = deep["conflicts"]

    # Phase 3b — Консолидация: слить дубли среди актуальных фактов user_memory.
    consolidations = await _consolidate(user_id)

    await _set_last_processed(max_msg_id)

    # Отчёт о прогоне (миграция 196) — best-effort, не валит цикл.
    impact_score = round(promoted / max(1, len(cands)), 4)
    await _write_report(
        user_id,
        candidates=len(cands),
        promoted=promoted,
        consolidations=consolidations,
        conflicts=conflicts,
        dream_text=dream_text,
        impact_score=impact_score,
    )

    log.info(
        "reflection.cycle_done",
        user_id=user_id,
        docs=len(docs),
        candidates=len(cands),
        promoted=promoted,
        consolidations=consolidations,
        conflicts=conflicts,
        dream=dream_written,
        impact=impact_score,
    )
    return {
        "status": "ok",
        "candidates": len(cands),
        "promoted": promoted,
        "consolidations": consolidations,
        "conflicts": conflicts,
        "dream": dream_written,
        "impact_score": impact_score,
    }


async def _rem(client: Any, user_id: int, cands: list[dict[str, Any]]) -> str:
    """REM-фаза: нарратив повторяющихся тем → reflection(kind='dream').

    Возвращает текст нарратива (или ``""`` при сбое/пустом) — для записи в
    ``dream_report.dream_text``. Вызывающий трактует непустую строку как «сон записан».
    """
    from app.dreams import add_reflection  # noqa: PLC0415
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
    src_ids: list[int] = []
    for c in top:
        src_ids.extend(c.get("message_ids") or [])
    try:
        await add_reflection(
            user_id, narrative, kind="dream", source_message_ids=src_ids[:30] or None
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("reflection.rem_store_failed", error=str(exc))
        return ""
    return narrative


async def _deep_sleep(
    user_id: int,
    cands: list[dict[str, Any]],
    threshold: float,
    min_recall: int,
    now: datetime,
) -> dict[str, int]:
    """Deep Sleep: скоринг + промоут высоких в user_memory через reconcile_and_add.

    Возвращает ``{promoted, conflicts}``: ``conflicts`` — сколько промоутов
    разрешили противоречие (mem0 update/delete старого факта).
    """
    from app.chat.user_memory import reconcile_and_add  # noqa: PLC0415

    relevance = _centrality(cands)
    known = await _already_known(user_id)
    promoted = 0
    conflicts = 0
    for i, cand in enumerate(cands):
        consolidated = _is_consolidated(_key_tokens(cand["text"]), known)
        score = _score(cand, relevance.get(i, 0.5), consolidated, now)
        distinct_sessions = len(cand["sources"])
        if score <= threshold or distinct_sessions < min_recall:
            continue
        try:
            res = await reconcile_and_add(user_id, cand["text"], kind=cand["kind"])
        except Exception as exc:  # noqa: BLE001 — промоут одного факта не валит цикл
            log.debug("reflection.promote_failed", error=str(exc))
            continue
        action = res.get("action")
        if action in ("add", "update"):
            promoted += 1
            await _reindex_sources(user_id, cand)
            # S6: из промоутнутого факта вытащить триплеты и достроить граф знаний
            # (сущности + рёбра kg_edge). Best-effort — не валит цикл сна.
            await _extract_graph(user_id, cand["text"])
        if action in ("update", "delete"):
            conflicts += 1
    return {"promoted": promoted, "conflicts": conflicts}


async def _extract_graph(user_id: int, fact_text: str) -> None:
    """Достроить семантический граф знаний из промоутнутого факта (S6, best-effort)."""
    try:
        from app.knowledge_graph import extract_entities_and_edges  # noqa: PLC0415

        await extract_entities_and_edges(user_id, fact_text, source_kind="dream")
    except Exception as exc:  # noqa: BLE001 — граф опционален, не валит цикл сна
        log.debug("reflection.graph_failed", error=str(exc))


# ── Phase 3b: Консолидация — слияние дублей в постоянной памяти ───────────────


async def _consolidate(user_id: int) -> int:
    """Слить дубли среди актуальных фактов user_memory (Jaccard ключевых токенов
    ≥ 0.5). Старые дубли soft-invalidate (superseded_by representative), новый
    representative реиндексируем best-effort. Возвращает число слитых фактов.
    """
    try:
        from app.chat.user_memory import consolidate_memories  # noqa: PLC0415

        merges = await consolidate_memories(user_id, threshold=0.5)
    except Exception as exc:  # noqa: BLE001 — консолидация не валит цикл
        log.debug("reflection.consolidate_failed", error=str(exc))
        return 0
    total = 0
    for m in merges:
        merged_ids = m.get("merged_ids") or []
        total += len(merged_ids)
        log.info(
            "reflection.consolidated",
            user_id=user_id,
            rep_id=m.get("rep_id"),
            merged=len(merged_ids),
        )
        await _reindex_memory(user_id, int(m.get("rep_id") or 0), str(m.get("rep_text") or ""))
    return total


async def _reindex_memory(user_id: int, mem_id: int, text: str) -> None:
    """Реиндекс/эмбеддинг слитого representative-факта (best-effort, опц.).

    Сам факт уже в ``user_memory`` (всегда в контексте через ``build_memory_block``),
    поэтому отдельная векторная строка ему не нужна — векторный recall ходит по
    ``chat_message`` (JOIN по id), синтетический id туда не ляжет осмысленно. Здесь
    лишь «прогреваем» эмбеддинг компакт-текста (если sqlite-vec/Ollama доступны),
    чтобы последующий recall встретил тёплую embed-модель. No-op без зависимостей.
    """
    if mem_id <= 0 or len(text) < 6:
        return
    try:
        from app.memory_vec import embed, sqlite_vec_available  # noqa: PLC0415

        if not sqlite_vec_available():
            return
        await embed(text)  # тёплый эмбеддинг компакт-факта (без orphan-строк в индексе)
    except Exception as exc:  # noqa: BLE001 — реиндекс опционален
        log.debug("reflection.reindex_memory_failed", error=str(exc))


# ── Отчёт о прогоне (миграция 196) ───────────────────────────────────────────


async def _write_report(
    user_id: int,
    candidates: int,
    promoted: int,
    consolidations: int,
    conflicts: int,
    dream_text: str,
    impact_score: float,
) -> None:
    """Записать строку dream_report (best-effort; на старой БД без таблицы — тихо)."""
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO dream_report(user_id, candidates, promoted, "
                "consolidations, conflicts, dream_text, impact_score) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    user_id,
                    int(candidates),
                    int(promoted),
                    int(consolidations),
                    int(conflicts),
                    dream_text or None,
                    float(impact_score),
                ),
            )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001 — отчёт опционален, не валит цикл
        log.debug("reflection.report_failed", error=str(exc))


async def _reindex_sources(user_id: int, cand: dict[str, Any]) -> None:
    """Реиндексировать исходные chat-сообщения промоутнутого факта (best-effort).

    Промоут пишется в ``user_memory`` (всегда в контексте через
    ``build_memory_block``); дополнительно «прогреваем» векторный индекс исходных
    реплик, чтобы episodic-recall брал доказательную базу на следующий день
    (docs §3.2 «После промоута — index_message»). No-op без sqlite-vec/Ollama.
    """
    msg_ids = (cand.get("message_ids") or [])[:3]
    if not msg_ids:
        return
    try:
        from app.memory_vec import index_message, sqlite_vec_available  # noqa: PLC0415

        if not sqlite_vec_available():
            return
        async with get_connection() as conn:
            placeholders = ",".join("?" * len(msg_ids))
            cur = await conn.execute(
                "SELECT m.id, m.content, m.session_id, m.created_at "
                f"FROM chat_message m WHERE m.id IN ({placeholders})",
                msg_ids,
            )
            rows = await cur.fetchall()
        for r in rows:
            await index_message(
                int(r["id"]),
                user_id,
                str(r["content"] or ""),
                session_id=r["session_id"],
                created_at=r["created_at"],
            )
    except Exception as exc:  # noqa: BLE001 — реиндекс опционален
        log.debug("reflection.reindex_failed", error=str(exc))


async def _set_last_processed(max_msg_id: int) -> None:
    """Запомнить max id обработанного chat-сообщения (анти-двойной-промоут)."""
    if max_msg_id <= 0:
        return
    try:
        async with get_connection() as conn:
            await set_kv(conn, _KV_LAST_MSG_ID, str(int(max_msg_id)))
    except Exception as exc:  # noqa: BLE001
        log.debug("reflection.marker_failed", error=str(exc))


__all__ = ["run_dream_cycle"]
