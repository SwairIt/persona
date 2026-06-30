"""Кабинет: настоящий граф памяти пользователя.

Узлы строятся из РЕАЛЬНЫХ данных владельца:
  * чаты (chat_session) и их сообщения (chat_message) — промпты и ответы;
  * сжатие: если у сессии задан summary_up_to_id, старые сообщения помечены
    compressed=true, а сам конспект — отдельный узел «summary»;
  * карточки памяти (hourly_card) — узлы «память», а те, где была речь
    (audio_seconds>0 / transcript_excerpt) — узлы «запись» (что говорил);
  * узлы-дни — сообщения/карточки группируются по дате.

Связи — структурные: сообщение↔сессия, промпт→ответ (пара), сообщение/карточка→
день, сессия→день, summary→сессия; плюс эвристические entity-связи по именам.
S6: поверх этого — РЕАЛЬНЫЙ семантический граф знаний (узлы-сущности + рёбра
``kg_edge`` с подписью ``relation_type``), который строит ночная рефлексия из
durable-фактов (``app/knowledge_graph.py``).

Роуты под owner-gate (auth_gate уже пускает сюда только владельца).
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["memory-graph"])

_MAX_MESSAGES = 220   # последние N сообщений (чтобы граф был живой, но не тяжёлый)
_MAX_CARDS = 60

# слова с большой буквы, которые НЕ являются именами/сущностями (частые начала
# предложений и общие термины) — не связываем по ним узлы графа.
_ENTITY_STOP: frozenset[str] = frozenset({
    "это", "вот", "если", "когда", "потом", "сейчас", "также", "можно", "нужно",
    "давай", "хорошо", "ладно", "спасибо", "привет", "конечно", "persona",
    "ты", "мне", "как", "что", "там", "они", "она", "для", "при", "persona",
    "да", "нет", "ок", "итак", "кстати", "например", "однако", "просто",
})


def _short(text: str | None, n: int = 46) -> str:
    t = (text or "").strip().replace("\n", " ")
    return (t[: n - 1] + "…") if len(t) > n else t


def _day(ts: str | None) -> str:
    return (ts or "")[:10]


@router.get("/graph", response_class=HTMLResponse)
async def graph_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "memory_graph.html",
        {"title": "Граф памяти", "active_nav": "graph"},
    )


@router.get("/api/graph.json")
async def graph_data(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    uid = session["user_id"]
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, str]] = []
    seen_days: set[str] = set()

    def day_node(ts: str | None) -> str | None:
        d = _day(ts)
        if not d:
            return None
        nid = f"d{d}"
        if d not in seen_days:
            seen_days.add(d)
            nodes.append({
                "id": nid, "type": "day", "label": d,
                "at": d, "where": "День записи",
                "full": "Все скриншоты, звук и карточки памяти за этот день.",
                "href": f"/timeline?date={d}",
            })
        return nid

    async with get_connection() as conn:
        # --- сессии ---
        cur = await conn.execute(
            "SELECT id, title, created_at, summary, summary_up_to_id "
            "FROM chat_session WHERE user_id = ? ORDER BY id",
            (uid,),
        )
        sessions = await cur.fetchall()
        sess_ids = [int(s["id"]) for s in sessions]
        titles: dict[int, str] = {}
        summary_cut: dict[int, int] = {}
        for s in sessions:
            sid = int(s["id"])
            title = s["title"] or f"Чат {sid}"
            titles[sid] = title
            nodes.append({
                "id": f"s{sid}", "type": "session",
                "label": _short(title, 30),
                "at": str(s["created_at"] or ""), "where": "Чат",
                "full": title, "href": f"/chat/{sid}",
            })
            dn = day_node(s["created_at"])
            if dn:
                links.append({"a": f"s{sid}", "b": dn})
            cut = s["summary_up_to_id"]
            if cut and s["summary"]:
                summary_cut[sid] = int(cut)
                nodes.append({
                    "id": f"sum{sid}", "type": "summary",
                    "label": "сжато: " + _short(s["summary"], 38),
                    "at": str(s["created_at"] or ""),
                    "where": f"Конспект чата «{_short(title, 40)}»",
                    "full": str(s["summary"]), "href": f"/chat/{sid}",
                })
                links.append({"a": f"sum{sid}", "b": f"s{sid}"})

        # --- сообщения (последние N) ---
        msgs: list[Any] = []
        if sess_ids:
            ph = ",".join("?" * len(sess_ids))
            cur = await conn.execute(
                f"SELECT id, session_id, role, content, created_at "  # noqa: S608 (ph — числа из БД)
                f"FROM chat_message WHERE session_id IN ({ph}) "
                f"ORDER BY id DESC LIMIT ?",
                (*sess_ids, _MAX_MESSAGES),
            )
            msgs = list(await cur.fetchall())
            msgs.reverse()

        present = {int(m["id"]) for m in msgs}
        prev_user_by_session: dict[int, int] = {}
        for m in msgs:
            mid = int(m["id"])
            sid = int(m["session_id"])
            role = m["role"]
            is_user = role == "user"
            compressed = sid in summary_cut and mid <= summary_cut[sid]
            nodes.append({
                "id": f"m{mid}",
                "type": "prompt" if is_user else "answer",
                "label": _short(m["content"], 46),
                "compressed": compressed,
                "at": str(m["created_at"] or ""),
                "where": ("Твой вопрос" if is_user else "Ответ Persona")
                + f" в чате «{_short(titles.get(sid, f'Чат {sid}'), 36)}»",
                "full": str(m["content"] or "")[:1200],
                "href": f"/chat/{sid}?msg={mid}",
            })
            links.append({"a": f"m{mid}", "b": f"s{sid}"})
            dn = day_node(m["created_at"])
            if dn:
                links.append({"a": f"m{mid}", "b": dn})
            # пара промпт→ответ: ответ ассистента к последнему промпту в сессии
            if is_user:
                prev_user_by_session[sid] = mid
            else:
                pu = prev_user_by_session.get(sid)
                if pu in present:
                    links.append({"a": f"m{pu}", "b": f"m{mid}"})

        # --- СМЫСЛОВЫЕ связи: сообщения, где упоминается одно и то же имя/
        # сущность (с большой буквы), соединяем между собой. Так граф
        # «связывается» по людям и темам (напр. все сообщения про «Олег»). ---
        ent_to_mids: dict[str, list[int]] = {}
        for m in msgs:
            mid = int(m["id"])
            ents = set(re.findall(r"\b[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё-]{2,}\b", m["content"] or ""))
            for e in ents:
                low = e.lower()
                if low in _ENTITY_STOP:
                    continue
                ent_to_mids.setdefault(low, []).append(mid)
        ent_link_count = 0
        for _ent, mids in ent_to_mids.items():
            uniq = list(dict.fromkeys(mids))
            if len(uniq) < 2:
                continue
            # цепочкой (не полный граф), чтобы не плодить рёбра
            for a, b in zip(uniq, uniq[1:]):
                links.append({"a": f"m{a}", "b": f"m{b}", "kind": "entity"})
                ent_link_count += 1
                if ent_link_count >= 250:
                    break
            if ent_link_count >= 250:
                break

        # --- карточки памяти / записи ---
        cur = await conn.execute(
            "SELECT hour_start, summary, transcript_excerpt, audio_seconds "
            "FROM hourly_card ORDER BY hour_start DESC LIMIT ?",
            (_MAX_CARDS,),
        )
        cards = await cur.fetchall()
        for c in cards:
            hs = c["hour_start"]
            spoke = (c["audio_seconds"] or 0) > 0 or bool((c["transcript_excerpt"] or "").strip())
            label = _short(c["transcript_excerpt"] or c["summary"], 44) or "час памяти"
            nid = f"h{hs}"
            day = _day(hs)
            full_parts = []
            if (c["summary"] or "").strip():
                full_parts.append(str(c["summary"]).strip())
            if (c["transcript_excerpt"] or "").strip():
                full_parts.append("Речь: " + str(c["transcript_excerpt"]).strip())
            if c["audio_seconds"]:
                full_parts.append(f"Звука: ~{int(c['audio_seconds'] // 60)} мин")
            nodes.append({
                "id": nid,
                "type": "recording" if spoke else "memory",
                "label": label,
                "at": str(hs or ""),
                "where": "Запись (экран + звук)" if spoke else "Карточка памяти",
                "full": "\n\n".join(full_parts) or "Час записи без расшифровки.",
                "href": f"/timeline?date={day}" if day else "/timeline",
            })
            dn = day_node(hs)
            if dn:
                links.append({"a": nid, "b": dn})

    # --- семантический граф знаний (S6): РЕАЛЬНЫЕ сущности + рёбра kg_edge ---
    # Узлы-сущности рисуем типом "memory" (зарегистрирован в TYPES фронта, поэтому
    # фильтруется/рендерится), а связи — с подписью relation_type (поле rel).
    try:
        from app.knowledge_graph import list_edges  # noqa: PLC0415

        kg_edges = await list_edges(uid, limit=400)
    except Exception:  # noqa: BLE001 — нет таблицы/модуля → граф без рёбер знаний
        kg_edges = []
    ent_seen: set[int] = set()
    for e in kg_edges:
        for eid, ename in (
            (e["from_entity_id"], e["from_name"]),
            (e["to_entity_id"], e["to_name"]),
        ):
            if eid in ent_seen:
                continue
            ent_seen.add(eid)
            nodes.append({
                "id": f"e{eid}", "type": "memory", "entity": True,
                "label": _short(ename, 30),
                "at": "", "where": "Сущность графа знаний",
                "full": str(ename),
                "href": f"/entity/{eid}",
            })
        links.append({
            "a": f"e{e['from_entity_id']}",
            "b": f"e{e['to_entity_id']}",
            "kind": "kg",
            "rel": e["relation_type"],          # подпись отношения
            "strength": e["strength"],
        })

    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["type"]] = counts.get(n["type"], 0) + 1

    return JSONResponse({
        "nodes": nodes,
        "links": links,
        "counts": counts,
        "truncated": len(msgs) >= _MAX_MESSAGES,
    })


__all__ = ["router"]
