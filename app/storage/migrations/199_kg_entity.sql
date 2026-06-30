-- 199_kg_entity.sql — отдельная таблица сущностей графа знаний (фикс потери рёбер).
--
-- БАГ (S6): таблица `entity` несёт ОДНОВРЕМЕННО два UNIQUE-констрейнта —
-- старый UNIQUE(name, kind) (миграция 110, глобальный extractor пишет туда
-- строки с user_id=NULL) и новый UNIQUE(user_id, name) (индекс idx_entity_user_name,
-- миграция 197, под персональный граф знаний). _upsert_entity делал
-- INSERT ... ON CONFLICT(user_id, name): при коллизии имени с ГЛОБАЛЬНОЙ строкой
-- extractor'а нарушался СТАРЫЙ UNIQUE(name, kind), а ON CONFLICT(user_id, name) его
-- НЕ ловил → IntegrityError → ребро молча терялось. Граф не писал рёбра при
-- пересечении имён.
--
-- ФИКС («отдельная таблица»): сущности графа знаний живут в СВОЕЙ таблице
-- kg_entity с единственным ключом UNIQUE(user_id, name) — без пересечения
-- констрейнтов с глобальной `entity`. Таблицу `entity` и entity_extractor НЕ
-- трогаем (они продолжают работать как раньше). kg_edge.from_entity_id/
-- to_entity_id теперь ссылаются на kg_entity.id (kg_edge в проде пуст — данные
-- не теряем).
--
-- Идемпотентно (IF NOT EXISTS). Без расширений.

-- Сущности графа знаний: одна строка на (пользователь, имя). Отдельно от
-- глобальной `entity`, чтобы старый UNIQUE(name, kind) не валил персональный апсерт.
CREATE TABLE IF NOT EXISTS kg_entity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT,                            -- грубый класс: 'person' / 'topic' (без CHECK)
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, name)
);

-- Сущности конкретного пользователя (обход графа от узла / список для /graph).
CREATE INDEX IF NOT EXISTS idx_kg_entity_user
    ON kg_entity(user_id);

-- kg_edge (миграция 197) ссылался FK на старую `entity`. Переводим ссылки на
-- kg_entity. SQLite не умеет ALTER ... DROP/ADD CONSTRAINT → нужна пересборка
-- таблицы. ВАЖНО: раннер миграций ПРОГОНЯЕТ КАЖДЫЙ .sql на КАЖДОМ старте
-- (idempotent-replay, без таблицы applied), поэтому безусловный DROP стирал бы
-- рёбра при каждом рестарте. Делаем пересборку через копию-перенос (kg_edge_v2):
-- копируем существующие строки → дропаем старую → переименовываем. Так FK
-- переезжает на kg_entity, а данные СОХРАНЯЮТСЯ при любом числе прогонов.
-- (kg_edge сейчас пуст до первой ночной рефлексии — но фикс безопасен и позже.)
CREATE TABLE IF NOT EXISTS kg_edge_v2 (
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
    FOREIGN KEY (from_entity_id) REFERENCES kg_entity(id) ON DELETE CASCADE,
    FOREIGN KEY (to_entity_id) REFERENCES kg_entity(id) ON DELETE CASCADE
);

-- Перенести существующие рёбра (если есть) в новую таблицу с FK на kg_entity.
INSERT INTO kg_edge_v2 (id, user_id, from_entity_id, to_entity_id, relation_type,
                        strength, source_kind, source_id, created_at, valid_until)
    SELECT id, user_id, from_entity_id, to_entity_id, relation_type,
           strength, source_kind, source_id, created_at, valid_until
    FROM kg_edge;

DROP TABLE IF EXISTS kg_edge;
ALTER TABLE kg_edge_v2 RENAME TO kg_edge;

-- Лента рёбер пользователя (только актуальные).
CREATE INDEX IF NOT EXISTS idx_kg_edge_user
    ON kg_edge(user_id) WHERE valid_until IS NULL;

-- Исходящие рёбра сущности (обход графа от узла).
CREATE INDEX IF NOT EXISTS idx_kg_edge_from
    ON kg_edge(from_entity_id) WHERE valid_until IS NULL;
