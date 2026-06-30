"""Тесты графа знаний (S6) — фикс тихой потери рёбер + дедуп отношений.

Без сети: ``extract_entities_and_edges`` зовёт LLM, поэтому тестируем напрямую
внутренние ``_upsert_entity`` / ``_upsert_edge`` через ``write_transaction`` —
ровно тот путь, на котором раньше терялось ребро.

Покрываем:
  (а) коллизия имени с ГЛОБАЛЬНОЙ строкой ``entity`` (extractor, user_id=NULL) +
      старый UNIQUE(name, kind) больше НЕ роняет апсерт сущности графа знаний →
      ребро записывается (раньше IntegrityError → ребро молча терялось);
  (б) дубль relation «работает в» / «Работает_В» нормализуется в одно ребро со
      strength=2 (а не два дубль-ребра).
"""

from __future__ import annotations

import pytest

from app.knowledge_graph import _upsert_edge, _upsert_entity, list_edges
from app.storage.db import get_connection, write_transaction


@pytest.mark.asyncio
async def test_edge_written_despite_global_entity_name_clash(db):
    """(а) Глобальная entity('Denis','person') не должна ронять граф знаний."""
    # Глобальная строка extractor'а: тот же name 'Denis', что и в графе знаний.
    # Раньше это и валило _upsert_entity по старому UNIQUE(name, kind).
    await db.execute(
        "INSERT INTO entity(name, kind, user_id) VALUES('Denis','person',NULL)"
    )
    await db.commit()

    # Записываем сущности графа знаний (своя таблица kg_entity) и ребро между ними.
    async with write_transaction() as conn:
        from_id = await _upsert_entity(conn, 5, "Denis")
        to_id = await _upsert_entity(conn, 5, "Acme")
        await _upsert_edge(conn, 5, from_id, to_id, "работает в", "test", None)

    # Раньше ребро терялось (IntegrityError → log.debug). Теперь оно есть.
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM kg_edge WHERE user_id = 5"
        )
        row = await cur.fetchone()
    assert int(row["c"]) > 0

    # И сущности графа знаний живут в kg_entity (отдельно от глобальной entity).
    edges = await list_edges(5)
    assert len(edges) == 1
    assert edges[0]["from_name"] == "Denis"
    assert edges[0]["to_name"] == "Acme"
    assert edges[0]["relation_type"] == "работает в"


@pytest.mark.asyncio
async def test_relation_dedup_strengthens_single_edge(db):
    """(б) «работает в» и «Работает_В» → одно ребро strength=2, не два дубля."""
    async with write_transaction() as conn:
        from_id = await _upsert_entity(conn, 7, "Denis")
        to_id = await _upsert_entity(conn, 7, "Acme")
        # Разный регистр + '_' вместо пробела + лишние пробелы — всё один relation.
        await _upsert_edge(conn, 7, from_id, to_id, "работает в", "test", None)
        await _upsert_edge(conn, 7, from_id, to_id, "Работает_В", "test", None)

    edges = await list_edges(7)
    assert len(edges) == 1, "дубль relation должен усиливать, а не плодить ребро"
    assert edges[0]["strength"] == pytest.approx(2.0)
    assert edges[0]["relation_type"] == "работает в"
