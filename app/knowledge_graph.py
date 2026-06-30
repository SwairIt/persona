"""Семантический граф знаний — LLM-извлечение триплетов (subject-relation-object).

Слайс S6. Поверх курируемой памяти (``app/chat/user_memory.py``) добавляет
СВЯЗИ между сущностями: из durable-факта вытаскивается триплет
``(субъект)-[отношение]->(объект)`` через GBNF-схему Ollama (как ``_extract_facts``),
сущности апсертятся в ОТДЕЛЬНУЮ таблицу ``kg_entity`` (UNIQUE ``user_id+name``,
миграция 199), а связь — строкой ``kg_edge`` с ``relation_type``/``strength``.

ВАЖНО (фикс потери рёбер): сущности графа знаний живут в своей таблице
``kg_entity``, а НЕ в глобальной ``entity``. У ``entity`` параллельно действуют
два UNIQUE — старый ``(name, kind)`` (миграция 110, глобальный extractor) и
``(user_id, name)`` (миграция 197); апсерт ``ON CONFLICT(user_id, name)`` не ловил
конфликт по старому ключу → ``IntegrityError`` → ребро молча терялось. Своя
таблица убирает пересечение констрейнтов.

bi-temporal как у ``user_memory``: ребро не удаляется, а soft-invalidate
(``valid_until=datetime('now')``) — история сохраняется, актуальные =
``valid_until IS NULL``. Повторный триплет не плодит дубль, а усиливает
существующее ребро (``strength += 1``).

Всё best-effort: нет Ollama / нет ``complete_json`` (облачный провайдер) / битый
ответ → тихий no-op (``log.debug`` + return), вызывающий цикл не падает.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection, write_transaction

log = get_logger("persona.knowledge_graph")

_MAX_NAME = 80          # длина имени сущности
_MAX_RELATION = 60      # длина типа отношения
_MAX_TRIPLES = 6        # сколько триплетов максимум берём из одного факта

# Сущности с такими «именами» — мусор (общие слова / местоимения), не узлы графа.
_ENTITY_STOP: frozenset[str] = frozenset({
    "это", "вот", "он", "она", "они", "оно", "ты", "вы", "мы", "я",
    "что", "кто", "как", "там", "тут", "здесь", "сейчас", "потом",
    "user", "пользователь", "ассистент", "persona", "никто", "ничто",
})

# GBNF-схема триплетов для Ollama complete_json (как _FACTS_SCHEMA в user_memory):
# format=schema физически отрезает мусор/битый JSON на слабой 4–8B модели.
_TRIPLES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "triples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "relation": {"type": "string"},
                    "object": {"type": "string"},
                },
                "required": ["subject", "relation", "object"],
            },
        }
    },
    "required": ["triples"],
}

_SYS_TRIPLES = (
    "Ты строишь граф знаний о пользователе. Из факта выпиши связи в виде троек "
    "(субъект, отношение, объект): кто/что — как связан — с кем/чем. Субъект и "
    "объект — короткие имена сущностей (человек, проект, место, вещь), отношение "
    "— глагол/тип связи («работает в», «знаком с», «любит», «находится в»). "
    "Только явные связи из факта, без домыслов. Пустой массив, если связей нет."
)


def _norm_name(text: str) -> str:
    """Нормализовать имя сущности: схлопнуть пробелы, обрезать длину."""
    return " ".join((text or "").split())[:_MAX_NAME]


def _norm_relation(rel: str) -> str:
    """Нормализовать тип отношения для сравнения/вставки (фикс дубль-рёбер).

    Trim + схлопнуть пробелы + lower + '_'→' ', чтобы «работает в», «Работает_В»
    и «работает  в» считались ОДНИМ отношением (иначе плодятся дубль-рёбра вместо
    усиления strength). Обрезаем до ``_MAX_RELATION``.
    """
    rel = (rel or "").replace("_", " ").lower()
    return " ".join(rel.split())[:_MAX_RELATION]


def _classify(name: str) -> str:
    """Грубо классифицировать сущность (entity.kind ограничен CHECK'ом миграции 110).

    1–2 слова Title-case → ``person``; иначе → ``topic``. Дёшево, без LLM.
    """
    parts = name.split()
    if 1 <= len(parts) <= 2 and all(p[:1].isupper() for p in parts if p):
        return "person"
    return "topic"


async def _extract_triples(client: Any, fact_text: str) -> list[dict[str, str]]:
    """Вытащить триплеты из факта через GBNF (только Ollama). [] при сбое/без схемы."""
    if not hasattr(client, "complete_json"):  # GBNF-схема — только Ollama
        return []
    from app.llm.client import CompletionRequest  # noqa: PLC0415

    try:
        out = await client.complete_json(
            CompletionRequest(
                system=_SYS_TRIPLES
                + " Верни JSON {triples:[{subject,relation,object}]}.",
                user=f"Факт: {fact_text}",
                max_tokens=300,
                temperature=0.0,
            ),
            _TRIPLES_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 — Ollama лёг / битый JSON → пропускаем
        log.debug("knowledge_graph.extract_failed", error=str(exc))
        return []
    res: list[dict[str, str]] = []
    for t in (out.get("triples") or []):
        if not isinstance(t, dict):
            continue
        subj = _norm_name(str(t.get("subject") or ""))
        rel = " ".join(str(t.get("relation") or "").split())[:_MAX_RELATION]
        obj = _norm_name(str(t.get("object") or ""))
        if not subj or not rel or not obj:
            continue
        if subj.casefold() in _ENTITY_STOP or obj.casefold() in _ENTITY_STOP:
            continue
        if subj.casefold() == obj.casefold():
            continue
        res.append({"subject": subj, "relation": rel, "object": obj})
        if len(res) >= _MAX_TRIPLES:
            break
    return res


async def _upsert_entity(conn: Any, user_id: int, name: str) -> int:
    """Апсерт сущности графа знаний по (user_id, name). Возвращает kg_entity.id.

    Пишем в ОТДЕЛЬНУЮ таблицу ``kg_entity`` (UNIQUE(user_id, name), миграция 199),
    а не в глобальную ``entity`` — у той параллельно действует старый
    UNIQUE(name, kind), из-за которого ``ON CONFLICT(user_id, name)`` ловил не тот
    конфликт и ребро терялось. Своя таблица убирает пересечение констрейнтов.
    """
    kind = _classify(name)
    await conn.execute(
        "INSERT INTO kg_entity(user_id, name, kind) VALUES(?,?,?) "
        "ON CONFLICT(user_id, name) DO NOTHING",
        (user_id, name, kind),
    )
    cur = await conn.execute(
        "SELECT id FROM kg_entity WHERE user_id = ? AND name = ?", (user_id, name)
    )
    row = await cur.fetchone()
    if row is None:  # pragma: no cover — только что вставили
        raise RuntimeError("kg_entity upsert produced no row")
    return int(row["id"])


async def _upsert_edge(
    conn: Any,
    user_id: int,
    from_id: int,
    to_id: int,
    relation: str,
    source_kind: str,
    source_id: int | None,
) -> None:
    """Апсерт ребра: повтор того же триплета усиливает (strength += 1), не дублирует.

    relation_type нормализуется через ``_norm_relation`` (trim/схлоп/lower/'_'→' ')
    И при сравнении, И при вставке — чтобы «работает в» и «Работает_В» считались
    одним отношением и усиливали strength, а не плодили дубль-ребро. Сравнение
    среди АКТУАЛЬНЫХ рёбер (``valid_until IS NULL``).
    """
    relation = _norm_relation(relation)
    cur = await conn.execute(
        "SELECT id FROM kg_edge WHERE user_id = ? AND from_entity_id = ? "
        "AND to_entity_id = ? AND relation_type = ? "
        "AND valid_until IS NULL LIMIT 1",
        (user_id, from_id, to_id, relation),
    )
    existing = await cur.fetchone()
    if existing:
        await conn.execute(
            "UPDATE kg_edge SET strength = strength + 1 WHERE id = ?",
            (int(existing["id"]),),
        )
        return
    await conn.execute(
        "INSERT INTO kg_edge(user_id, from_entity_id, to_entity_id, relation_type, "
        "strength, source_kind, source_id) VALUES(?,?,?,?,?,?,?)",
        (user_id, from_id, to_id, relation, 1.0, source_kind, source_id),
    )


async def extract_entities_and_edges(
    user_id: int,
    fact_text: str,
    source_kind: str = "dream",
    source_id: int | None = None,
) -> int:
    """Из durable-факта вытащить триплеты, апсертнуть сущности и рёбра графа знаний.

    Best-effort: нет Ollama / нет GBNF / битый ответ → тихий no-op (0). Возвращает
    число записанных/усиленных рёбер. Никогда не поднимает наружу — вызывается из
    ночного цикла рефлексии, который не должен падать.
    """
    fact_text = " ".join((fact_text or "").split())
    if len(fact_text) < 6:
        return 0

    # LLM-клиент (туннель/Ollama). Нет клиента → нет извлечения → тихий no-op.
    try:
        from app.llm.client import make_client  # noqa: PLC0415

        client = make_client(kind="chat_summary")
    except Exception as exc:  # noqa: BLE001 — LLMNotConfigured и пр.
        log.debug("knowledge_graph.no_llm", error=str(exc))
        return 0

    triples = await _extract_triples(client, fact_text)
    if not triples:
        return 0

    edges = 0
    try:
        async with write_transaction() as conn:
            for t in triples:
                try:
                    from_id = await _upsert_entity(conn, user_id, t["subject"])
                    to_id = await _upsert_entity(conn, user_id, t["object"])
                    await _upsert_edge(
                        conn, user_id, from_id, to_id, t["relation"],
                        source_kind, source_id,
                    )
                    edges += 1
                except Exception as exc:  # noqa: BLE001 — одно ребро не валит транзакцию-факт
                    # warning (а не debug): сбой записи ребра = тихая потеря данных
                    # графа, это надо видеть в логах, а не молча проглатывать.
                    log.warning("knowledge_graph.edge_failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — нет таблиц (старая БД) / БД занята → no-op
        log.debug("knowledge_graph.store_failed", error=str(exc))
        return 0
    if edges:
        log.info("knowledge_graph.edges_added", user_id=user_id, edges=edges)
    return edges


async def list_edges(user_id: int, limit: int = 400) -> list[dict[str, Any]]:
    """Актуальные рёбра графа знаний пользователя с именами сущностей (для /graph).

    Best-effort: на старой БД без таблицы ``kg_edge`` — пустой список (тихо).
    """
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT e.id, e.from_entity_id, e.to_entity_id, e.relation_type, "
                "       e.strength, ef.name AS from_name, et.name AS to_name "
                "FROM kg_edge e "
                "JOIN kg_entity ef ON ef.id = e.from_entity_id "
                "JOIN kg_entity et ON et.id = e.to_entity_id "
                "WHERE e.user_id = ? AND e.valid_until IS NULL "
                "ORDER BY e.strength DESC, e.id DESC LIMIT ?",
                (user_id, max(1, min(2000, int(limit)))),
            )
            rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — нет таблицы → граф без рёбер знаний
        log.debug("knowledge_graph.list_failed", error=str(exc))
        return []
    return [
        {
            "id": int(r["id"]),
            "from_entity_id": int(r["from_entity_id"]),
            "to_entity_id": int(r["to_entity_id"]),
            "relation_type": str(r["relation_type"]),
            "strength": float(r["strength"]),
            "from_name": str(r["from_name"]),
            "to_name": str(r["to_name"]),
        }
        for r in rows
    ]


__all__ = ["extract_entities_and_edges", "list_edges"]
