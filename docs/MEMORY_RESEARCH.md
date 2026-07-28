# Память Persona — исследование и рекомендуемая архитектура

> Синтез 7 research-агентов (Letta/MemGPT, mem0/Zep/Graphiti/Cognee, Generative
> Agents + Reflexion, Hermes «Dreaming», Yandex Serverless-YDB-вебинар, паттерны
> для локальных 4–8B LLM, salience/forgetting) → конкретный план для Persona
> (FastAPI + aiosqlite WAL + Jinja/Alpine, локальная Ollama + nomic-embed-text).
>
> Заземлено в реальном коде Persona (актуально на `__version__ = 2.20.66`,
> последняя миграция `190`). Все ссылки на файлы/строки — фактические.

---

## 0. Что у Persona УЖЕ есть (точка отсчёта)

Прежде чем что-то строить — фиксируем текущий каркас, чтобы НЕ дублировать.

| Слой | Файл | Состояние |
|------|------|-----------|
| Keyword recall (FTS5 bm25 + LIKE fallback) | `app/chat/sessions.py` → `recall_by_terms` (L466), `recall_relevant` (L552), `_fts_expr` (L12) | ✅ работает, дефолт |
| Vector/Hybrid recall (sqlite-vec KNN + RRF k=60) | `app/memory_vec.py` → `hybrid_recall` (L276), `embed` (L56), `index_message` (L90), `backfill_index` (L197), опц. cross-encoder `_rerank` (L235) | ✅ опционально, тихий fallback |
| Личные факты («кто ты») + mem0-консолидация | `app/chat/user_memory.py` → `reconcile_and_add` (L264), `extract_and_store` (L364), `build_memory_block` (L415), bi-temporal `invalidate_memory` (L118) | ✅ есть ADD/UPDATE/DELETE/NOOP + soft-invalidate |
| Bi-temporal схема user_memory | `migrations/180_user_memory.sql`, `187_user_memory_bitemporal.sql` (`valid_until`, `superseded_by`) | ✅ |
| Vec-схема (vec0 FLOAT[768] + meta) | `migrations/186_vec_memory.sql` (`chat_message_vec`, `vec_message_meta`) | ✅ |
| Реакции (👍/👎 — сигнал Evaluator) | `sessions.py` → `set_reaction` (L572), `latest_reaction` (L594) | ✅ есть, для Reflexion |
| Режим recall (kv `recall_mode`) | `routes/chat_sessions.py` → `_get_recall_mode` (L272): дефолт `hybrid` если sqlite-vec есть, иначе `keyword` | ✅ |
| Точка интеграции recall в промпт | `routes/chat_sessions.py` L994–1031 (`build_memory_block` + recall-блок), L1519–1521 (`extract_and_store` после обмена) | ✅ |
| Паттерн воркеров | `app/workers/_bases.py` → `BackfillRunner`, `ClockScheduler` (per-date kv-маркер, idempotent); регистрация в `app/web/main.py` lifespan (L1170+) | ✅ образец `memory_of_day_worker.py`, `day_end_summary_scheduler.py`, `weekly_rollup_worker.py` |
| Реальные источники «за день» | `screenshots` (`ocr_text`, `app_name`, `captured_at`, `pinned_at`), `audio_segment` (`transcript`, `started_at`, `locale`), `chat_message` (`content`, `role`, `created_at`, `session_id`) | ✅ есть данные для «сна» |

**Вывод:** у Persona уже реализованы Фазы 1–2 многих исследованных систем. Бóльшая
часть плана ниже — это **достройка scoring-слоя, ночного воркера-рефлексии и RAG**,
а не переписывание с нуля.

---

## 1. Краткий обзор подходов

### 1.1 Letta / MemGPT — «LLM как ОС», память тремя ярусами
- **Core memory** — маленькие именованные блоки (`persona`, `human`), ВСЕГДА в
  контексте; компилируются в системный промпт каждый ход.
- **Recall memory** — полная история сообщений ВНЕ контекста, ищется по запросу.
- **Archival memory** — безграничное хранилище (вектор), агент сам пишет/читает.
- **Self-editing как tool-calls:** `core_memory_append/replace`,
  `archival_memory_insert/search`, `conversation_search`. Модель сама решает
  КОГДА звать — гигиена памяти это работа модели, не фиксированный пайплайн.
- **Heartbeat:** `request_heartbeat=true` → цепочка шагов (поиск → правка → ответ).
- **Sleep-time agent:** фоновый агент, делит memory-блоки с основным
  (`shared_block_ids`), переписывает их асинхронно (дедуп, консолидация). Можно
  более СИЛЬНОЙ моделью — он не на latency-пути.
- **Маппинг на Persona:** `user_memory` ≈ блок `human`; `chat_system_prompt`/
  `FRIEND_PROMPT` ≈ блок `persona`; sqlite-vec ≈ archival; FTS5 ≈ recall.

### 1.2 mem0 / Zep-Graphiti / Cognee — извлечение, темпоральный граф, дедуп
- **mem0 two-phase (главная воспроизводимая идея):** EXTRACT (LLM достаёт
  атомарные факты из summary + последних M=10 реплик) → UPDATE (на каждый
  кандидат: vector top-K=10 + LLM выбирает **ADD/UPDATE/DELETE/NOOP**). Это даёт
  дедуп+разрешение конфликтов за один проход. **Persona это УЖЕ сделала** —
  `reconcile_and_add` в `user_memory.py`.
- **mem0 отказался от типизированного графа** (Neo4j) → «hub-and-spoke»:
  таблица entity→memory, без триплетов. Урок: граф-БД дал мало; join-таблица
  «сущность↔сообщение» ловит почти всё.
- **Zep/Graphiti bi-temporal:** 4 метки — `t_valid`/`t_invalid` (когда факт был
  истинен в мире) и `t_created`/`t_expired` (когда попал/ушёл из БД). Конфликт =
  не удаление, а штамп `t_invalid` (**Persona УЖЕ**: `valid_until`+`superseded_by`).
- **Graphiti дедуп-фастпас:** MinHash+LSH ловит near-duplicate дёшево, LLM — только
  на спорном остатке (держит токены под контролем).
- **Cognee:** ECL-пайплайн + `ontology_valid` флаг (отсев галлюцинированных
  сущностей по whitelist). Минус: хочет 3 хранилища.
- **Брать алгоритмы, НЕ инфраструктуру:** bi-temporal-штамп, invalidate-not-delete,
  дедуп-ограниченный-парой, fan-out+rerank — поверх FTS5 + sqlite-vec.

### 1.3 Generative Agents (Stanford) + Reflexion — scoring и саморефлексия
- **Retrieval score (точная формула):**
  `score = a_recency·recency + a_importance·importance + a_relevance·relevance`,
  каждый компонент **min-max нормирован в [0,1]** по кандидатам, потом сумма; в
  статье **все a=1**.
  - `recency = 0.995 ^ hours_since_last_access` (decay по часам с **последнего
    доступа**, не создания — recall «греет» память).
  - `importance` — LLM-оценка 1–10 при создании (промпт «poignancy 1–10»),
    нормируется `/10`.
  - `relevance` — косинус эмбеддинга запроса и памяти.
- **Reflection-триггер:** сумма importance последних событий > **150** → рефлексия.
- **Reflection-цепочка (2 шага):** (a) из последних N=100 памятей → «3 самых важных
  вопроса»; (b) на каждый вопрос retrieval → «5 инсайтов (because of 1,5,3)».
  Инсайты пишутся обратно в поток как НОВЫЕ памяти (дерево рефлексий).
- **Reflexion (вербальное подкрепление, без обучения):** Actor → Evaluator
  (reward/пас-фейл) → Self-Reflection (текстовый вывод «что пошло не так»);
  буфер рефлексий (Ω=1–3) подмешивается в следующую попытку. У Persona Evaluator
  уже есть — `chat_reaction` (👎).

### 1.4 Persona «Dreaming» — проектный алгоритм ночной консолидации
> **Уточнение 2026-07-28:** это не shipped-функция Hermes Agent. В официальном
> репозитории Auto Dream пока описан как открытый feature request
> [#10771](https://github.com/NousResearch/hermes-agent/issues/10771). Фазы,
> дефолты и формула ниже — выбранный дизайн Persona, а не спецификация Hermes.

- **Триггер:** cron `0 3 * * *` (3:00), skip если активность за `quiet_minutes=60`.
  Opt-in. Дефолты: `lookback_days=7`, `max_candidates=50`,
  `promotion_threshold=0.6`, `min_recall_count=2`.
- **3 фазы сна:**
  1. **Light Sleep** — скан транскриптов за `lookback_days`, дедуп, СТЕЙДЖ
     кандидатов (без записи в постоянное хранилище).
  2. **REM** — извлечь повторяющиеся темы/паттерны → нарратив в `DREAMS.md`
     (недеструктивно).
  3. **Deep Sleep** — скоринг каждого кандидата, ПРОМОУТ высоких в `MEMORY.md`.
- **Формула скоринга (взвешенная сумма 0..1):** relevance 30% + frequency 24% +
  query-diversity 15% + recency 15% + consolidation 10% (ШТРАФ за дубль уже в
  памяти) + richness 6%. Промоут если `score>0.6 И recall_count≥2`.
- **Недеструктивный стейджинг (DREAMS) → деструктивный промоут (MEMORY)** =
  аудируемый след.

### 1.5 Yandex — «память агента в ОДНОЙ serverless-БД (YDB)»
- Тезис: вся память + retrieval на ОДНОЙ БД вместо отдельного vector-DB + инфры.
  Семантический поиск по данным И истории диалогов внутри БД; RAG по своим
  источникам как анти-галлюцинация; **переиспользование ответов LLM** (semantic
  answer-cache) для экономии; MCP-сервер поверх БД как инструмент агента.
- **Для Persona:** это **зелёный свет на sqlite-вектор-в-том-же-файле**
  (`memory_vec.py`) — не выносить память во внешний vector-DB. Плюс две идеи на
  будущее: (a) **semantic answer-cache** перед вызовом Ollama; (b) **MCP-сервер
  поверх sqlite-памяти** (`recall`/`search` как tools).

### 1.6 Локальные 4–8B LLM через Ollama — что реально работает
- **Эмбеддинг тащит retrieval, не генератор** — вкладывай качество туда. nomic =
  ок для англ.; для RU/EN/DE лучше `qwen3-embedding:0.6b` (1024-dim, мультиязычный).
- **ПРЕФИКСЫ ОБЯЗАТЕЛЬНЫ** (тихий убийца качества): nomic требует
  `search_document:` для хранимого и `search_query:` для запроса. Ollama НЕ
  добавляет их сам → −5..10 пунктов retrieval. **⚠️ В Persona сейчас НЕ
  добавляются** (`memory_vec.py:embed` L73).
- **`num_ctx` по умолчанию 2048**, даже если модель держит 8192 → длинные чанки
  режутся ДО эмбеддинга. Надо явно поднять.
- **Less-is-more:** малые модели «теряются в середине» → top-k маленький (4–6),
  важное — в НАЧАЛО или КОНЕЦ промпта, не в середину.
- **Hybrid > vector-only** (RRF k=60) — keyword/FTS это легитимная primary-
  стратегия на CPU, не «костыль».
- **Структурный вывод фактов:** schema + один пример + строгие правила + temp 0–0.1
  + **constrained decoding** (Ollama `format=schema`/GBNF). Persona УЖЕ так делает
  (`_FACTS_SCHEMA`, `complete_json`).

### 1.7 Salience / forgetting / privacy
- **Retrieval-score** = тот же Generative-Agents (recency·importance·relevance).
- **Decay** = `decay^(hours_since_last_access)`, half-life по типу: волатильные
  session-факты — часы/дни; профильные — месяцы/никогда. Recall сбрасывает
  «часы» (rehearsal, эффект Эббингауза).
- **Importance** при ЗАПИСИ: LLM 1–10 ИЛИ эвристика (буст за durable-факты/имена/
  решения; даунвейт за смолток). Хранить и не пересчитывать.
- **Access-frequency reinforcement:** `access_count` + кольцо последних доступов;
  частые памяти сопротивляются забыванию.
- **Forgetting = decay × frequency × importance**; eviction: TTL, salience-порог,
  консолидация. **Soft-delete > hard-delete** (`superseded_by`/`valid_to`/`reason`).
- **Two-phase write:** быстрый ONLINE-приём + медленный OFFLINE-консолидатор (cron).
- **Privacy:** PII-редакция ДО хранения (типизированные плейсхолдеры `<EMAIL_1>`);
  минимизация (хранить durable, не сырые транскрипты); Right-To-Be-Forgotten —
  hard-purge строки + её FTS + sqlite-vec индекс; local-first + at-rest шифрование.

---

## 2. РЕКОМЕНДУЕМАЯ архитектура памяти Persona

### 2.1 Четыре яруса (привязка к существующим хранилищам)

```
┌─ WORKING (в контексте, всегда) ─────────────────────────────┐
│ persona-блок = chat_system_prompt / FRIEND_PROMPT           │
│ human-блок    = build_memory_block(user_memory)             │
│ + memory-statistics строка (#recall, #archival, #facts)     │
│ + FIFO-буфер последних реплик сессии (+ рекурсивный summary) │
└─────────────────────────────────────────────────────────────┘
┌─ EPISODIC (что было сказано/снято/услышано) ────────────────┐
│ chat_message (+ chat_message_fts) = «диалоговая» история     │
│ screenshots.ocr_text, audio_segment.transcript = «жизнь дня» │
│ recall: FTS5 bm25 + sqlite-vec KNN → RRF (hybrid_recall)     │
└─────────────────────────────────────────────────────────────┘
┌─ SEMANTIC (факты/знание, которые решено помнить) ───────────┐
│ user_memory (bi-temporal, kind=fact/preference/person/...)  │
│ + (новое) entities + entity_mention (hub-and-spoke)         │
│ + (новое, опц.) rag_chunk — RAG по документам пользователя  │
└─────────────────────────────────────────────────────────────┘
┌─ PROCEDURAL (как себя вести / выводы о пользователе) ───────┐
│ reflections — инсайты ночной рефлексии (Generative-Agents)  │
│ self_notes — Reflexion-буфер «что я делаю не так» (Ω≤3)     │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 Единый recall (оставляем и усиливаем `hybrid_recall`)

Архитектура recall уже верная (FTS5 bm25 + vector KNN → RRF k=60). Достраиваем
**scoring-пересортировку** поверх кандидатов, которые `hybrid_recall` уже достал:

```
финальный_score(memory) =
    a_recency   · norm(0.995 ^ hours_since_last_seen)
  + a_importance· norm(importance / 10)
  + a_relevance · norm(rrf_or_cosine)          # уже считается
  [+ a_entity   · entity_overlap]              # опц., из hub-and-spoke
  [- echo_penalty]                             # _is_echo уже есть
→ min-max нормировка по кандидатному набору, top-k=4–6, затем MMR-проход
  (разнообразие, чтобы не 5 перефразов одного факта).
```

- Веса `a_*` — kv (`recall_w_recency/_importance/_relevance`, дефолт 1.0 — как в
  статье), правятся на `/settings/memory`.
- Реализация: **новая функция `score_and_rerank()` в `app/memory_vec.py`**,
  вызывается в конце `hybrid_recall` ПЕРЕД `_fmt`. В keyword-режиме (`recall_by_terms`)
  можно применить ту же пересортировку, заменив relevance на bm25-ранг.
- `keyword` остаётся ДЕФОЛТОМ (правило CLAUDE.md), `generative` — новый opt-in
  режим recall_mode, который включает importance/recency-веса.

### 2.3 Scoring-поля (миграции)

**Миграция `191_memory_salience.sql`** — колонки на `chat_message` (для
episodic-скоринга) и на `user_memory` (для semantic-скоринга):

```sql
-- 191_memory_salience.sql — salience/recency/частота для scoring recall.
-- Идемпотентно (раннер глотает duplicate column). Без расширений.
ALTER TABLE chat_message  ADD COLUMN importance   INTEGER;          -- 1..10, NULL=не оценено
ALTER TABLE chat_message  ADD COLUMN last_seen     TEXT;            -- ISO, бамп при recall
ALTER TABLE chat_message  ADD COLUMN access_count  INTEGER NOT NULL DEFAULT 0;

ALTER TABLE user_memory   ADD COLUMN salience      REAL;            -- 1..10 (llm|эвристика)
ALTER TABLE user_memory   ADD COLUMN importance_source TEXT;        -- 'llm'|'heuristic'
ALTER TABLE user_memory   ADD COLUMN last_seen     TEXT;
ALTER TABLE user_memory   ADD COLUMN access_count  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user_memory   ADD COLUMN redacted      INTEGER NOT NULL DEFAULT 0;  -- PII-флаг

CREATE INDEX IF NOT EXISTS idx_user_memory_salience
    ON user_memory(user_id, salience DESC) WHERE valid_until IS NULL;
```

> Bi-temporal (`valid_until`, `superseded_by`) у `user_memory` УЖЕ есть (миграция
> 187) — это и есть Graphiti-style invalidate-not-delete. Достраивать не нужно.

**Миграция `192_reflections.sql`** — procedural-ярус (рефлексии + dream-дневник):

```sql
-- 192_reflections.sql — инсайты ночной рефлексии (Generative-Agents reflection
-- tree) + собственный REM-дневник «снов» Persona.
CREATE TABLE IF NOT EXISTS reflection (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'insight',   -- insight|dream|self_note
    text TEXT NOT NULL,
    source_message_ids TEXT,                 -- JSON-массив id (цитаты «because of …»)
    importance REAL,                         -- 1..10
    valid_until TEXT,                        -- soft-invalidate, как user_memory
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reflection_active
    ON reflection(user_id, kind, id DESC) WHERE valid_until IS NULL;
```

**Миграция `193_entities.sql`** (опц., hub-and-spoke вместо графа):

```sql
-- 193_entities.sql — лёгкий entity-слой (mem0 hub-and-spoke, НЕ типизированный граф).
CREATE TABLE IF NOT EXISTS entity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT,                               -- person|project|place|org|...
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS entity_mention (
    entity_id INTEGER NOT NULL,
    message_id INTEGER,                       -- ссылка на chat_message
    memory_id INTEGER,                        -- ИЛИ на user_memory
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (entity_id) REFERENCES entity(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entity_mention ON entity_mention(entity_id);
```

---

## 3. НОЧНОЙ ВОРКЕР-РЕФЛЕКСИЯ («сон / обучение» как у Hermes)

### 3.1 Где живёт и как встроен

**Новый файл `app/workers/dream_worker.py`** — обёртка над существующим
`ClockScheduler` (тот же паттерн, что `memory_of_day_worker.py`):

```python
# app/workers/dream_worker.py — ночная консолидация памяти («сон»), Hermes-style.
scheduler = ClockScheduler(
    name="dream",
    hour_local_getter=_hour_getter,       # kv dream_hour_local, дефолт 3 (03:00)
    enabled_getter=_enabled_getter,       # kv dream_enabled, дефолт "0" (OPT-IN)
    marker_kv="dream_last_fired",         # per-date идемпотентность (как у всех)
    job=_run_dream_cycle,
    poll_seconds=1800,                    # 30-мин тик ловит окно HH:00–HH:59
)
```

Регистрация — одна строка в `app/web/main.py` lifespan (рядом с L1276
`_run_memory_of_day_worker`):

```python
asyncio.create_task(_run_dream_worker(controller), name="dream-worker"),
```

**Гейт активности (quiet_minutes):** в начале `_run_dream_cycle` — SELECT
`MAX(created_at)` из `chat_message`; если активность за последние 60 мин → skip
(перенос на следующий тик). Это Hermes `quiet_minutes=60`.

**Идемпотентность сверх per-date маркера:** kv `dream_last_processed_message_id` —
чтобы пропущенная ночь/ручной перезапуск не промоутили дважды (паттерн Letta
`last_processed_message_id`).

### 3.2 Что делает по шагам (3 фазы сна)

**Источники за `lookback_days=7` (реальные таблицы Persona):**
- Диалоги: `chat_message JOIN chat_session` (роль user, `created_at` за окно).
- Экран/OCR: `screenshots.ocr_text` WHERE `captured_at` за окно (топ по объёму/
  закреплённости — `pinned_at IS NOT NULL` приоритетнее).
- Аудио: `audio_segment.transcript` WHERE `started_at` за окно.

**Фаза 1 — Light Sleep (стейдж кандидатов, БЕЗ записи):**
1. Выбрать сообщения/OCR/транскрипты за окно, сгруппировать по сессии/дню.
2. Дешёвый путь кандидатов — переиспользовать FTS5-слой (`recall_by_terms`) для
   салиентных терминов; лучший путь — **1 Ollama-вызов на сессию** «выпиши durable-
   факты/предпочтения/сущности» (тот же `_extract_facts` + `_FACTS_SCHEMA` из
   `user_memory.py`, GBNF-надёжность).
3. Дедуп кандидатов: нормализованный хэш текста (быстрый путь) → для спорных
   sqlite-vec-косинус против существующих (Graphiti-style фастпас-затем-LLM).
4. `max_candidates=50` — ограничить прогон.

**Фаза 2 — REM (нарратив тем, недеструктивно):**
- Отправить кластеры кандидатов в Ollama (chat-модель через
  `make_client(kind="chat_summary")`): «выдели повторяющиеся темы/паттерны за
  неделю». Записать нарратив + `source_message_ids` в таблицу `reflection`
  (`kind='dream'`). НИЧЕГО не трогает в `user_memory`.

**Фаза 3 — Deep Sleep (скоринг + промоут):**
- На каждый кандидат — взвешенный score (Hermes-формула):
  `relevance·0.30 + frequency·0.24 + diversity·0.15 + recency·0.15 +
  consolidation·0.10 + richness·0.06`, где `consolidation = 1 −
  already_in_user_memory` (штраф за дубль).
- Промоут в `user_memory` если `score>0.6 И встречен в ≥2 сессиях`.
- Промоут идёт через **существующий `reconcile_and_add`** (ADD/UPDATE/DELETE/NOOP +
  bi-temporal soft-invalidate) — не плодим дубли, разрешаем конфликты.
- Перед промоутом эмбеддить кандидат и косинус-сравнить с `user_memory`; при
  высоком сходстве — `access_count += 1` (reinforcement), а не вставка.
- После промоута — `index_message`/эмбеддинг нового факта, чтобы recall брал его
  на следующий день.

**Опц. Generative-Agents reflection поверх Фазы 3** (если включён):
- Сумма `importance` свежих событий > порога → цепочка «3 вопроса → 5 инсайтов
  (because of …)» → запись инсайтов в `reflection` (`kind='insight'`) +
  эмбеддинг. Это «дерево рефлексий».

### 3.3 Промпты к Ollama (готовые)

**Извлечение durable-фактов (Light Sleep) — переиспользует `_FACTS_SCHEMA`:**
```
system: «Ты ведёшь долговременную память личного ассистента. Из транскрипта за день
(чат + что было на экране + расшифровка речи) выпиши ТОЛЬКО durable-факты/
предпочтения/важных людей/проекты/цели. НЕ включай: сиюминутное, вопросы, общие
знания. Кратко, от 3-го лица. Верни JSON {facts:[{text,kind}]}; пустой массив если
фактов нет.»  (temp 0.0, format=_FACTS_SCHEMA)
```

**REM-нарратив (темы недели):**
```
system: «Подведи итог: какие повторяющиеся темы и паттерны видны в этих заметках за
неделю? 2–4 коротких наблюдения о пользователе, от 3-го лица, без воды.»
```

**Importance-оценка (Generative-Agents, batched — несколько в одном вызове):**
```
«По шкале 1–10, где 1 — обыденность (почистил зубы), 10 — крайне значимо
(расставание, поступление), оцени значимость каждой памяти. Память: <text>
Оценка: <int>»
```

**Reflection-цепочка:** шаг A «3 самых важных вопроса о пользователе по этим
утверждениям»; шаг B «5 инсайтов (формат: инсайт (because of 1,5,3))».

### 3.4 Стоимость/латентность
- Importance — **батчить** (несколько памятей в одном промпте), писать best-effort,
  дефолт 3 при сбое парсинга, кэшировать (не пересчитывать).
- Рефлексия — burst-вызовы, но воркер вне hot-path (ночью), модель удалённая через
  devtunnel — поэтому ночной batch правильный дефолт (а не Letta every-N-steps).
- Любой сбой Ollama (туннель лёг) → тихий fallback, как везде в `memory_vec.py`.

---

## 4. План внедрения (по фазам, на реальные файлы)

Каждая фаза независимо отгружаема и уважает правило «keyword по умолчанию, vector
opt-in». Релиз-ритуал на КАЖДЫЙ фич-коммит: бамп `app/__init__.py __version__` +
`CACHE_VERSION` в `app/web/static/sw.js`.

### Фаза A — Фиксы качества эмбеддингов (дёшево, высокий ROI)
**Файлы:** `app/memory_vec.py`.
1. **Префиксы nomic** в `embed()` (L56) — параметр `kind: str`:
   `search_document:` для `index_message`/`backfill_index`, `search_query:` для
   `hybrid_recall`. ⚠️ нужен **реиндекс** (`backfill_index`) после изменения.
2. **`num_ctx=8192`** в теле POST `/api/embeddings` (L71–74).
3. (Опц.) kv `embed_model` → оценить `qwen3-embedding:0.6b` (1024-dim) для RU/DE;
   при смене dim — новая vec0-таблица (миграция) + полный реиндекс.

### Фаза B — Salience-scoring recall
**Файлы:** миграция `191`; `app/memory_vec.py` (`score_and_rerank`),
`app/chat/sessions.py` (бамп `last_seen`/`access_count` на surfaced rows),
`routes/chat_sessions.py` (`_RECALL_MODES` + `generative`).
1. Миграция 191 (см. §2.3).
2. `score_and_rerank()` в `memory_vec.py` — формула §2.2, веса из kv.
3. На КАЖДОМ recall — `UPDATE … SET last_seen=now, access_count=access_count+1`
   для отданных в блок памятей (rehearsal/decay-reset). Один UPDATE, без Ollama.
4. Новый `recall_mode='generative'` в `_get_recall_mode` (L272) + `_RECALL_MODES`.

### Фаза C — Importance при записи
**Файлы:** `app/chat/user_memory.py` (salience в `reconcile_and_add`/
`extract_and_store`), точка вызова `routes/chat_sessions.py` L1519.
1. При записи факта — LLM 1–10 (batched) ИЛИ эвристика (durable→высоко,
   смолток→низко); писать в `user_memory.salience` + `importance_source`.
2. Best-effort, fallback на эвристику при недоступной Ollama.

### Фаза D — Ночной воркер-рефлексия (ядро запроса)
**Файлы:** миграция `192`; новый `app/workers/dream_worker.py`; новый
`app/chat/reflection.py` (логика 3 фаз + промпты); регистрация в
`app/web/main.py` (L~1276); новый `app/dreams.py`-репозиторий для таблицы
`reflection`.
1. Миграция 192 (§2.3).
2. `dream_worker.py` через `ClockScheduler` (§3.1), opt-in kv `dream_enabled`.
3. `reflection.py` — `run_dream_cycle()`: Light→REM→Deep (§3.2), промоут через
   `reconcile_and_add`.
4. Подмешать свежий `kind='insight'`/`kind='dream'` в `build_memory_block`
   (`user_memory.py` L415) отдельным блоком «что я заметил о тебе».

### Фаза E — RAG по источникам + Reflexion-петля (опц.)
**Файлы:** миграция `194_rag_chunk.sql`; `app/chat/rag.py`; интеграция блока в
`routes/chat_sessions.py` рядом с recall-блоком (L1005).
1. Пользователь прикрепляет документы → чанк+эмбеддинг в sqlite-vec → top-k
   отдельным контекст-блоком (анти-галлюцинация, Yandex-тезис).
2. **Reflexion:** на 👎-реакцию (`latest_reaction` L594) — Self-Reflection-промпт →
   `reflection` (`kind='self_note'`, буфер Ω≤3) → подмешать в системный промпт.

### Фаза F — UI/settings + privacy
**Файлы:** `routes/settings_hub.py` (категория есть, L34/L108 — `/settings/memory`),
страница `/settings/memory`, новый роут.
1. `/settings/memory`: просмотр/правка/удаление памятей, веса `a_*`, decay
   half-life, toggle `dream_enabled`/час, **кнопка «Забыть всё»** (hard-purge
   строки + FTS + sqlite-vec индекс — RTBF).
2. PII-редакция перед записью в `user_memory` (типизированные плейсхолдеры; reuse
   подход из аудита кредов 6a806dc).

---

## 5. Что реально работает на локальной Ollama 4–8B без файнтюна (и что нет)

### ✅ Работает (строить можно)
- **Эмбеддинги + retrieval, hybrid (FTS5+vector RRF)** — основа, уже есть.
- **Извлечение нескольких полей constrained-JSON** (`format=schema`/GBNF) — у
  Persona уже надёжно (`complete_json`, `_FACTS_SCHEMA`).
- **mem0 ADD/UPDATE/DELETE/NOOP** на маленьком наборе кандидатов — уже работает.
- **Importance 1–10** одним коротким (батч-)промптом.
- **Короткая rollup/holistic-суммаризация** окна (регенерация всего summary из
  prev+new, не append) — на коротком окне ок.
- **Recall пары-горстки фактов** + reflection ОФФЛАЙН (ночью, не на latency-пути).
- **Reflexion вербальное подкрепление** (буфер 1–3) — на реакциях.

### ❌ Хрупко / не делать без сильной модели
- Сложное multi-document reasoning (4B заметно слабее 7B).
- Опора на длинный эффективный контекст (>8–16K) — «теряются в середине».
- Надёжный free-form JSON БЕЗ grammar-constraints.
- Multi-hop агентный «memory manager» с самозапускающимися циклами на hot-path
  (Letta every-N-steps) — для удалённой-через-туннель модели это латентность;
  поэтому выбран **ночной batch**, не онлайн-агент.
- Типизированный knowledge-graph (Neo4j-стиль) — mem0 доказал, что не окупается;
  берём hub-and-spoke join-таблицу.
- Полный community-detection / 3-store пайплайны (Graphiti/Cognee) — лишняя инфра.

---

## 6. Чеклист задач для реализации (Ф3)

**Фаза A — эмбеддинги (1 коммит):**
- [ ] `memory_vec.embed(text, kind)` — `search_document:`/`search_query:` префиксы.
- [ ] `num_ctx=8192` в POST `/api/embeddings`.
- [ ] Реиндекс через `backfill_index` после изменения (документировать в роуте).
- [ ] (Опц.) оценить `qwen3-embedding:0.6b`, новая vec0-dim + полный реиндекс.

**Фаза B — scoring recall (1–2 коммита):**
- [ ] Миграция `191_memory_salience.sql` (importance/last_seen/access_count/salience/redacted).
- [ ] `memory_vec.score_and_rerank()` — recency·importance·relevance, min-max, MMR.
- [ ] Вызвать из `hybrid_recall` перед `_fmt`; те же веса опц. в `recall_by_terms`.
- [ ] Бамп `last_seen`/`access_count` для отданных памятей на каждом recall.
- [ ] `recall_mode='generative'` в `_get_recall_mode` + `_RECALL_MODES`.
- [ ] kv-веса `recall_w_recency/_importance/_relevance` (дефолт 1.0).

**Фаза C — importance при записи (1 коммит):**
- [ ] LLM 1–10 (batched) ИЛИ эвристика → `user_memory.salience` + `importance_source`.
- [ ] Fallback на эвристику при недоступной Ollama.

**Фаза D — ночной воркер «сон» (2–3 коммита, ядро):**
- [ ] Миграция `192_reflections.sql` (таблица `reflection`).
- [ ] `app/dreams.py` — репозиторий reflection (add/list/invalidate).
- [ ] `app/chat/reflection.py` — `run_dream_cycle()`: Light → REM → Deep + промпты §3.3.
- [ ] Гейт `quiet_minutes=60` (MAX(created_at) из chat_message) + kv
      `dream_last_processed_message_id`.
- [ ] `app/workers/dream_worker.py` через `ClockScheduler` (opt-in `dream_enabled`,
      `dream_hour_local`=3).
- [ ] Регистрация в `app/web/main.py` lifespan (рядом с L1276).
- [ ] Промоут кандидатов через `reconcile_and_add` (+ `access_count` reinforcement).
- [ ] Подмешать `kind='insight'/'dream'` в `build_memory_block` отдельным блоком.
- [ ] Дефолты kv: `dream_lookback_days=7`, `dream_max_candidates=50`,
      `dream_promotion_threshold=0.6`, `dream_min_recall_count=2`.

**Фаза E — RAG + Reflexion (опц., 1–2 коммита):**
- [ ] Миграция `194_rag_chunk.sql` + `app/chat/rag.py` (чанк/эмбед/top-k блок).
- [ ] Reflexion: 👎 (`latest_reaction`) → Self-Reflection → `reflection`
      (`self_note`, Ω≤3) → системный промпт.
- [ ] (На будущее) semantic answer-cache перед вызовом Ollama; MCP-сервер поверх памяти.

**Фаза F — UI/privacy (1 коммит):**
- [ ] Страница `/settings/memory`: просмотр/правка/удаление, веса, decay,
      `dream_enabled`/час, кнопка «Забыть всё» (hard-purge строка+FTS+vec).
- [ ] PII-редакция перед записью в `user_memory`.
- [ ] Категория в `settings_hub._CATEGORIES` уже есть (L108) — обновить описание.

**Сквозное (каждый коммит):**
- [ ] Бамп `app/__init__.py __version__` + `CACHE_VERSION` в `sw.js` (одинаковые).
- [ ] Логирование прогона «сна» через `diagnostics_bundle.py`/`doctor.py` (debuggable).
- [ ] Тихий fallback на keyword при любой недоступности Ollama/sqlite-vec.

---

### Приоритет (если делать по-одному)
1. **Фаза A** (префиксы + num_ctx) — копеечный фикс, чинит «тихую» деградацию recall.
2. **Фаза D** (ночной «сон») — главный запрос, максимальная ценность «памяти».
3. **Фаза B** (scoring) — делает recall заметно умнее.
4. **Фаза C → F** — по мере необходимости.
