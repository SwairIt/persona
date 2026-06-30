-- 197_kg_edges.sql — семантический граф знаний: рёбра (триплеты) поверх entity.
--
-- Слайс S6. Ночная рефлексия («сон») для каждого промоутнутого факта вытаскивает
-- LLM-триплеты subject-relation-object (app/knowledge_graph.py) и пишет их сюда:
-- сущности (subject/object) апсертятся в существующую таблицу `entity`, а связь
-- между ними — строкой kg_edge с типом отношения (relation_type) и силой.
--
-- bi-temporal как у user_memory: ребро не удаляется, а soft-invalidate через
-- valid_until=datetime('now') (история сохраняется, актуальные = valid_until IS NULL).
--
-- Таблица entity (миграция 110) глобальна и UNIQUE(name, kind) — без user_id.
-- Граф знаний персональный, поэтому добавляем `entity.user_id` (NULLable: старые
-- строки extractor'а остаются глобальными) + отдельный UNIQUE-индекс (user_id, name)
-- под апсерт графа. Существующий апсерт по (name, kind) НЕ затрагиваем.
--
-- Идемпотентно (IF NOT EXISTS; ADD COLUMN обёрнут — раннер глотает дубль-прогон).
-- Без расширений.

-- user_id для персонального графа знаний (старый extractor пишет NULL — глобально).
ALTER TABLE entity ADD COLUMN user_id INTEGER;

-- Апсерт сущностей графа знаний: одна строка на (пользователь, имя).
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_user_name
    ON entity(user_id, name);

-- Рёбра графа знаний: триплет (from_entity)-[relation_type]->(to_entity).
CREATE TABLE IF NOT EXISTS kg_edge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    from_entity_id INTEGER NOT NULL,
    to_entity_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL,          -- глагол/тип связи: «работает в», «знаком с»…
    strength REAL NOT NULL DEFAULT 1.0,   -- уверенность/частота (растёт при повторе)
    source_kind TEXT,                     -- откуда: 'dream' / 'chat' / 'manual'…
    source_id INTEGER,                    -- id источника (опц.)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    valid_until TEXT,                     -- bi-temporal: NULL = актуально
    FOREIGN KEY (from_entity_id) REFERENCES entity(id) ON DELETE CASCADE,
    FOREIGN KEY (to_entity_id) REFERENCES entity(id) ON DELETE CASCADE
);

-- Лента рёбер пользователя (только актуальные).
CREATE INDEX IF NOT EXISTS idx_kg_edge_user
    ON kg_edge(user_id) WHERE valid_until IS NULL;

-- Исходящие рёбра сущности (обход графа от узла).
CREATE INDEX IF NOT EXISTS idx_kg_edge_from
    ON kg_edge(from_entity_id) WHERE valid_until IS NULL;
