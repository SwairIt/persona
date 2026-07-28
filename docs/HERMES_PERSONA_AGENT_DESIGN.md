# Persona Agent: owner-only, Telegram, память и безопасные «сны»

Статус: design proposal, код этим документом не меняется.
Дата проверки источников: 2026-07-28.
Цель: единый личный агент Persona для сайта и Telegram с одной идентичностью,
историей, памятью, проактивными пробуждениями и офлайн-консолидацией.

## 1. Главный вывод

Persona уже имеет значительную часть необходимых механизмов: постоянные
`chat_session`/`chat_message`, FTS/vector recall, `user_memory`, bi-temporal
инвалидацию, ночной `dream_worker`, `reflection`, `dream_report`, граф знаний,
планировщики, инструменты и уведомления.

Но текущая реализация не является единым агентным ядром:

- основной сценарий разговора находится в огромном HTTP/SSE route;
- Telegram-адаптера и канонического channel/thread mapping нет;
- ночной цикл напрямую изменяет постоянную память и граф;
- доказательства неполные, особенно для OCR и аудио;
- планировщики живут внутри web-процесса;
- «сон» в документации неверно приписан штатному Hermes Agent;
- автоматические выводы модели могут затем сами попасть в prompt и усилить ошибку.

Правильная цель — не копия Hermes, а Persona Agent с теми же удачными
принципами: одно ядро на всех поверхностях, ограниченная curated memory,
поиск полной истории, фоновые reviews, планировщик, allowlist и наблюдаемость.
Собственная сильная сторона Persona — события реальной жизни из чата, экрана,
аудио и календаря — должна быть сохранена, но обёрнута provenance и политиками.

## 2. Что у Hermes подтверждено, а что нет

### 2.1 Подтверждено официальными источниками

Hermes Agent и семейство моделей Nous Hermes — разные сущности. Здесь сравнение
идёт с open-source framework **Hermes Agent**, а не с весами Hermes 3/4.

В Hermes Agent подтверждены:

- единое агентное ядро для CLI, gateway, API и messaging-платформ;
- Telegram, Discord, Slack и другие входные поверхности;
- постоянные сессии в SQLite, полный журнал сообщений и FTS5-поиск;
- ограниченная curated memory (`MEMORY.md` и `USER.md`);
- фоновый self-improvement review после хода, который может предложить/сохранить
  память или procedural skill;
- режим `write_approval`, в котором автоматические записи ставятся на одобрение;
- memory-provider lifecycle: prefetch до хода и sync после ответа;
- natural-language cron, изолированные cron-сессии, lock от двойного запуска и
  журнал попыток;
- Telegram allowlist по sender ID и отдельно по group chat ID;
- независимые Telegram topics/sessions и устойчивое сопоставление thread → session;
- инструменты, skills, MCP, делегирование и отдельные профили.

### 2.2 Не подтверждено как готовая функция Hermes

Автоматический Hermes Dream / `DREAMS.md` с фазами Light → REM → Deep,
формулой `30/24/15/15/10/6`, `quiet_minutes=60` и автоматическим promotion в
`MEMORY.md` **не найден в официальной документации и текущем репозитории как
штатная функция**.

Наоборот, в репозитории Hermes есть открытый feature request
“Automatic Memory Consolidation (Auto Dream)” (#10771). Он описывает возможный
будущий механизм, но имеет статус open. Поэтому раздел `1.4` в
`docs/MEMORY_RESEARCH.md` и комментарии “Hermes DREAMS.md equivalent” следует
считать проектной гипотезой Persona, а не воспроизведённым алгоритмом Hermes.

Практический вывод: текущий сон Persona можно развивать, но нельзя обосновывать
безопасность или качество тем, что «так сделано у Hermes».

### 2.3 Общие agent/memory patterns, а не функции Hermes

Следующие идеи полезны, но происходят из отдельных работ:

- Generative Agents: memory stream, retrieval по recency/importance/relevance,
  reflection как вывод более высокого уровня;
- MemGPT: working/core, recall и archival tiers;
- Reflexion: обучение поведению через текстовую рефлексию на проверяемом
  feedback, без изменения весов;
- Sleep-time Compute: офлайн-предвычисление полезного контекста до будущего
  запроса;
- staged consolidation с provenance: инженерный safety pattern, а не
  доказанная функция Hermes.

## 3. Текущее состояние Persona

### 3.1 Что уже можно переиспользовать

| Возможность | Текущая реализация |
|---|---|
| Канонические web-чаты | `app/chat/sessions.py`, migrations 158/168 |
| Cross-chat recall | FTS5 в `app/chat/sessions.py`, vector/hybrid в `app/memory_vec.py` |
| Curated user memory | `app/chat/user_memory.py` |
| Конфликты и история фактов | `valid_until`, `superseded_by` |
| Рефлексии и REM-тексты | `app/dreams.py`, migration 191 |
| Ночной цикл | `app/chat/reflection.py`, `app/workers/dream_worker.py` |
| Отчёт сна | migration 196 |
| Граф | `app/knowledge_graph.py`, migrations 197/199 |
| Планировщик | `ClockScheduler` в `app/workers/_bases.py` |
| Проактивный briefing | `app/briefing.py`, `app/workers/briefing_worker.py` |
| Внутренние уведомления | `app/notifications.py` |
| Owner resolution | `app/auth/owner.py` |
| Инструменты | `app/mcp/builtin_tools.py`, browser/workspace/MCP subsystems |

### 3.2 Критические ограничения текущего «сна»

1. **Нет полного provenance.** `source_message_ids` хранит только id сообщений
   чата. Для OCR и аудио источник теряется.
2. **OCR и аудио читаются из глобальных таблиц без `user_id`.** Это допустимо
   только при жёстком single-owner deployment.
3. **Нет staging.** Высоко оценённый LLM-кандидат сразу проходит
   `reconcile_and_add`, после чего может попасть в prompt следующих разговоров.
4. **REM-текст тоже автоматически попадает в prompt.** Ошибка модели становится
   будущим «наблюдением о пользователе».
5. **Неверная атрибуция автора алгоритма.** Формула и фазы названы Hermes, хотя
   подтверждения нет.
6. **Слабая идентификация говорящего.** Текст экрана и чужая речь могут быть
   ошибочно интерпретированы как факт о владельце.
7. **Неполные тесты.** Есть DB round-trip рефлексии, но нет end-to-end теста
   `run_dream_cycle`, ложных фактов, rollback, идемпотентности и падения между фазами.
8. **Глобальные маркеры.** `dream_last_processed_message_id` и scheduler marker
   хранятся в общем KV, а quiet check смотрит максимум по всем чатам.
9. **Граф теряет семантику доказательств.** `strength += 1` считает повторный
   LLM-вывод усилением, даже если он получен из того же источника.
10. **Лишние LLM-вызовы.** Факты сначала извлекаются, затем для каждого
    promoted факта отдельно извлекаются триплеты.

### 3.3 Ограничения текущего чата для Telegram

`app/web/routes/chat_sessions.py::api_send_stream` объединяет HTTP/SSE,
авторизацию, команды, recall, prompt assembly, вызовы инструментов, генерацию,
персистентность и постобработку. Telegram нельзя качественно подключить к этому
route без копирования логики или внутреннего HTTP-вызова.

До Telegram необходимо получить application use case, не зависящий от FastAPI:

```text
Web/SSE ───────┐
Telegram ──────┼─> ConversationService.handle_turn()
Cron/Wake ─────┘        │
                        ├─ Identity/Thread
                        ├─ Context/Memory
                        ├─ Model/Tools
                        ├─ Message repository
                        └─ Domain events + delivery outbox
```

## 4. Неподвижные инварианты

1. В deployment `persona.getdoday.ru` существует ровно один principal-владелец.
2. Никакой пользователь, подписка или роль не расширяют доступ в owner-only mode.
3. Один входящий Telegram update обрабатывается не более одного раза.
4. Один внешний channel/thread отображается не более чем на один активный
   канонический thread.
5. Сайт и Telegram используют один `ConversationService`, один system persona,
   одну память и одинаковый набор разрешённых инструментов.
6. Raw evidence неизменяемо; исправление создаёт новую revision/invalidation.
7. Любой автоматический durable-факт имеет ссылку хотя бы на одно доказательство.
8. Third-party сообщение не становится фактом о владельце без явного основания.
9. «Сон» не блокирует online-ответы и не выполняется параллельно сам с собой.
10. Любое изменение памяти или графа обратимо и видно владельцу.
11. Проактивный агент не выполняет внешние destructive/mutating actions без
    отдельной policy/approval.
12. Web-процесс не владеет тяжёлыми бесконечными worker loops.

## 5. Целевая Clean Architecture

Рекомендуемая структура:

```text
app/agent/
  domain/
    identity.py
    conversation.py
    memory.py
    graph.py
    events.py
    policies.py
  application/
    handle_turn.py
    ingest_event.py
    run_wake_cycle.py
    run_sleep_cycle.py
    approve_memory.py
  ports/
    model.py
    tools.py
    repositories.py
    scheduler.py
    delivery.py
    clock.py
  infrastructure/
    sqlite/
    llm/
    tools/

app/interfaces/
  web/
  telegram/
  worker/
```

Правило зависимостей:

- domain не импортирует FastAPI, Telegram SDK, SQLite или конкретный LLM;
- application зависит только от domain и ports;
- adapters преобразуют HTTP/Telegram/cron в application commands;
- infrastructure реализует ports;
- SQL не появляется в route/handler файлах;
- web и Telegram adapters не собирают prompt самостоятельно.

Переписывать всё сразу не нужно. Первый anti-corruption layer может обернуть
существующие repositories и LLM/tool runtime, после чего route постепенно станет
тонким адаптером.

## 6. Owner-only security model

### 6.1 Сайт

Нужен явный режим `PERSONA_OWNER_ONLY=1`, включённый для production:

- owner задаётся стабильным `owner_user_id`/email в защищённой конфигурации;
- fallback “минимальный id в users” запрещён в production;
- `full_access_user_ids`, роли и подписка игнорируются;
- signup/register для посторонних отключены;
- login неизвестного/не-owner аккаунта не выдаёт application session;
- существующие не-owner sessions отзываются;
- middleware на ошибке БД работает fail-closed, а не fail-open;
- каждый приватный route дополнительно использует owner dependency;
- публичными могут остаться только marketing/static/health endpoints;
- agent/worker endpoints принимают отдельные scoped machine tokens, а не
  пользовательскую cookie.

Следует сохранить break-glass доступ только через loopback/консольную команду,
не через публичный HTTP.

### 6.2 Telegram

Нужны три независимые проверки:

1. `telegram_user_id == configured_owner_telegram_id`;
2. `chat_id` входит в explicit allowed chat list;
3. для group reply выполняется mention/reply/wake policy.

Нельзя использовать “разрешён весь group chat” как авторизацию инструментов:
это позволит любому участнику управлять личным агентом. Сообщения других
участников могут быть наблюдаемым контекстом, но команды исполняет только owner.

Рекомендуемые настройки:

- DM: только owner ID;
- group: explicit chat allowlist;
- ответ: `require_mention=true` или reply к боту;
- passive observation: отдельный opt-in на каждый group;
- mutating tools в группах: deny по умолчанию;
- bot token никогда не логировать и не показывать в UI;
- webhook: проверять secret header; long polling: singleton lease;
- dedupe по `update_id`.

### 6.3 Ограничения Telegram

Обычный Bot API не даёт произвольно прочитать всю предыдущую историю чата.
Бот анализирует только updates, которые Telegram доставил после подключения.
При включённом privacy mode в группах он получает в основном команды,
упоминания и ответы; bot-admin получает все новые сообщения. Это необходимо
объяснять в UI и не обещать ретроспективный импорт.

Если позже использовать Telegram Secretary/Business connection, это должен быть
отдельный opt-in adapter с явным списком разрешённых чатов и отдельной privacy
политикой — не скрытое расширение полномочий обычного бота.

## 7. Единая identity и threading

### 7.1 Каноническая идентичность

Добавить:

- `principal`: внутренний owner principal;
- `channel_identity`: `(principal_id, platform, external_user_id)`;
- один Telegram owner ID жёстко связан с owner principal;
- неизвестный external ID не может создать principal автоматически.

### 7.2 Канонические threads

Сохранить `chat_session` как канонический conversation aggregate либо заменить
его новой сущностью только после миграции данных.

Добавить `channel_binding`:

```text
id
principal_id
platform              web | telegram | cron | api
external_chat_id
external_thread_id
chat_session_id
mode                  dm | group | topic | proactive
created_at
last_seen_at
UNIQUE(platform, external_chat_id, external_thread_id)
```

Правила:

- web thread и Telegram topic могут быть явно связаны с одной сессией;
- по умолчанию каждый Telegram DM/topic — отдельная session, но вся curated
  memory общая;
- `/new` создаёт новую session и атомарно обновляет binding;
- `/link <code>` связывает Telegram topic с выбранным web-thread через
  короткоживущий одноразовый код, созданный владельцем на сайте;
- group thread никогда автоматически не сливается с private DM;
- cron/wake запускается в отдельной session, а результат доставляется в
  настроенный home channel.

### 7.3 Origin metadata

У `chat_message` или связанной `message_origin` должны быть:

- platform/chat/thread;
- external message/update id;
- sender principal/external actor;
- reply-to;
- edit/delete state;
- ingestion timestamp и original timestamp;
- content classification;
- checksum/dedupe key.

Так сайт показывает сообщения Telegram, Telegram продолжает историю сайта, а
sleep pipeline понимает автора и происхождение текста.

## 8. Event journal и надёжная доставка

Не следует расширять существующий `sync_event`: сейчас он допускает только
несколько UI-сущностей и `apply_pending` фактически лишь ставит stamp.

Нужны отдельные сущности:

### 8.1 Inbox

`agent_inbox`:

- adapter/platform;
- external event id;
- encrypted/raw payload или ограниченный normalized payload;
- received/processed/failed timestamps;
- attempts/error;
- unique dedupe key;
- TTL для raw transport payload.

### 8.2 Domain event journal

`agent_event` — append-only:

- owner/principal;
- aggregate type/id;
- kind и schema version;
- correlation/causation id;
- source channel/message;
- payload;
- privacy class;
- occurred/recorded timestamps.

Примеры:

- `conversation.message.received`;
- `conversation.response.completed`;
- `memory.candidate.proposed`;
- `memory.revision.approved`;
- `dream.run.completed`;
- `graph.edge.superseded`;
- `wake.triggered`;
- `tool.action.requested/completed/denied`.

### 8.3 Outbox

`delivery_outbox`:

- target channel/chat/thread;
- payload/content reference;
- status `pending|leased|sent|failed|dead`;
- attempt count/next retry;
- Telegram message id после отправки;
- idempotency key.

Запись ответа и outbox должна происходить в одной DB-транзакции. Отдельный
delivery worker отправляет сообщение и помечает `sent`. Это даёт at-least-once
delivery с dedupe, переживает рестарт и не теряет проактивные сообщения.

## 9. ConversationService

`handle_turn(command)` должен выполнять единый pipeline:

1. авторизовать principal/channel policy;
2. разрешить channel binding и session;
3. идемпотентно сохранить user message;
4. распознать platform-independent command;
5. собрать system persona;
6. выполнить bounded memory prefetch;
7. собрать историю с token budget/compression;
8. запустить model/tool loop с step/time/cost limits;
9. инкрементально сохранять assistant message;
10. завершить ответ и создать outbox/domain events;
11. асинхронно поставить post-turn review;
12. вернуть stream событий адаптеру.

Web SSE и Telegram получают один поток доменных событий:

- `token`;
- `tool_started/tool_finished`;
- `memory_used`;
- `done`;
- `error`.

Telegram может редактировать одно “typing/streaming” сообщение раз в 0.7–1.5 с,
а не отправлять каждый token отдельным сообщением.

## 10. Модель памяти

### 10.1 Ярусы

1. **Working:** активный prompt/history текущей session.
2. **Episodic:** полные сообщения, capture events, OCR/audio fragments.
3. **Semantic:** атомарные факты, предпочтения, цели, люди, проекты.
4. **Relational:** temporally valid entity/edge graph.
5. **Procedural:** проверенные способы работы, tool preferences и инструкции.
6. **Reflective:** гипотезы/наблюдения, которые не равны подтверждённым фактам.

`dream`/`insight` нельзя без маркировки смешивать с semantic truth.

### 10.2 Evidence first

Каждая memory revision:

- имеет type;
- confidence;
- status `proposed|approved|active|rejected|superseded`;
- valid time и system time;
- provenance links;
- extractor/model/prompt version;
- created_by `user|agent|dream`;
- sensitivity;
- TTL/decay policy.

Доказательство хранится типизированно:

- chat message id;
- Telegram message identity;
- screenshot id + OCR span;
- audio segment id + speaker confidence/span;
- note/calendar/reminder id.

### 10.3 Trust policy источников

Приоритет по умолчанию:

1. явное “запомни” владельца;
2. повторённое владельцем утверждение;
3. сообщение владельца;
4. подтверждённая запись календаря/заметка;
5. owner speech с уверенным speaker attribution;
6. OCR;
7. слова другого участника;
8. LLM reflection.

Низкодоверенные источники могут создать candidate/episode, но не durable
semantic fact автоматически.

### 10.4 Post-turn review

После завершённого хода дешёвый background review может:

- предложить новый durable fact;
- предложить обновление/инвалидацию;
- записать task outcome/feedback;
- предложить procedural lesson.

Автоприменение допустимо только для узкого low-risk набора с высокой
уверенностью и owner-authored evidence. Всё остальное попадает в pending review.

Это соответствует полезному паттерну Hermes `write_approval`, но хранение
остаётся локальным и типизированным.

### 10.5 Retrieval

Online retrieval должен быть двухступенчатым:

1. deterministic candidate retrieval: FTS/vector/time/entity;
2. bounded rerank с provenance и diversity.

В prompt попадают:

- компактный approved profile;
- top relevant semantic memories;
- несколько episodic excerpts;
- отдельный блок “unverified reflections”, только если релевантен;
- ссылки на evidence для объяснимости.

Нельзя всегда инжектировать последние REM-нарративы независимо от вопроса.

## 11. Sleep / reflection cycles

### 11.1 Значение «сна»

Сон — не мистическая жизнь LLM и не обучение весов. Это offline jobs:

- нормализация и дедуп данных;
- extraction кандидатов;
- выявление повторяющихся паттернов;
- contradiction detection;
- graph proposals;
- precomputation будущего контекста;
- проверка качества retrieval;
- отчёт владельцу.

### 11.2 Триггеры

Запуск:

- по расписанию;
- после N новых событий;
- после завершения длинной session;
- при idle window;
- вручную;
- catch-up после downtime.

Гейт тишины должен учитывать owner activity на всех поверхностях, активную LLM
генерацию и DB pressure. Триггер создаёт job, но не исполняет LLM в scheduler tick.

### 11.3 Безопасный pipeline

1. **Orientation**
   - зафиксировать snapshot boundary/event cursor;
   - модель/config/prompt version;
   - проверить budget и lock.
2. **Gather**
   - собрать новые evidence после cursor;
   - не использовать собственные прошлые reflections как raw evidence.
3. **Normalize**
   - language/date/entity normalization;
   - speaker/source classification;
   - deterministic exact/near dedupe.
4. **Extract**
   - атомарные typed candidates с evidence spans;
   - отдельно facts, preferences, goals, relationships, procedures.
5. **Critique**
   - проверить entailment кандидата каждым evidence;
   - отметить contradiction/ambiguity/third-party.
6. **Consolidate**
   - предложить ADD/UPDATE/SUPERSEDE/NOOP;
   - не менять active memory.
7. **Graph**
   - предложить entity aliases и temporal edges;
   - связать с теми же evidence.
8. **Precompute**
   - summary открытых тем;
   - вероятные вопросы/следующие действия;
   - entity/project briefs.
9. **Evaluate**
   - прогнать golden recall queries;
   - сравнить before/after retrieval;
   - reject run при ухудшении или policy violation.
10. **Apply**
    - auto-apply только policy-safe candidates;
    - остальные pending;
    - атомарный revision batch.
11. **Report**
    - что найдено, изменено, отклонено;
    - ссылки на evidence;
    - cost/duration/model/errors.

### 11.4 Rollback

`dream_run` хранит список созданных revisions. Rollback:

- не удаляет raw evidence;
- делает новые invalidation/reinstatement revisions;
- восстанавливает предыдущий active projection;
- перестраивает graph projection;
- оставляет audit trail.

### 11.5 Что применять автоматически

Можно автоматически:

- точный дедуп одной и той же evidence;
- нормализацию пробелов/регистра/абсолютной даты;
- обновление access/usage counters;
- безопасную индексацию;
- approved deterministic TTL transitions.

Требует review:

- имя, здоровье, отношения, убеждения;
- выводы из речи другого человека;
- психологические характеристики;
- противоречивые факты;
- удаление/замена pinned memory;
- procedural change, расширяющий tool permissions.

## 12. Graph consolidation

Текущий `kg_entity`/`kg_edge` нужно развить:

- casefolded canonical key и alias table;
- entity merge/split revisions;
- relation ontology или хотя бы normalized relation family;
- `valid_from/valid_until` и `recorded_at/expired_at`;
- confidence;
- evidence join table;
- extractor version;
- unique evidence fingerprint;
- positive и negative/contradictory claims;
- strength как функция независимых evidence, а не числа LLM-повторов.

Ночной graph pipeline:

1. deterministic normalization;
2. alias candidate retrieval;
3. LLM entity resolution только для ambiguous pairs;
4. edge dedupe по independent evidence;
5. temporal contradiction groups;
6. community/project/person summaries;
7. staged merge proposals;
8. projection rebuild и consistency checks.

Граф не должен быть единственным источником истины. Он является projection
поверх evidence и memory revisions и может быть полностью перестроен.

## 13. Autowake / proactive agent

### 13.1 Триггеры

- due reminder/calendar;
- утренний briefing;
- незакрытый high-confidence commitment;
- важное изменение проекта/события;
- окончание sleep cycle;
- health/worker anomaly;
- явное owner rule “разбуди меня, когда …”.

### 13.2 Wake cycle

1. принять trigger event;
2. проверить owner policy, quiet hours и cooldown;
3. собрать минимальный relevant context;
4. решить `ignore|notify|ask|act`;
5. для `act` проверить tool permission/approval;
6. создать отдельную proactive session;
7. записать результат в outbox;
8. ждать feedback и использовать его в будущей полезности.

### 13.3 Антиспам

- дневной budget;
- cooldown на kind/entity;
- dedupe похожих сообщений;
- quiet hours;
- severity threshold;
- “почему я это отправил”;
- mute/snooze/disable;
- feedback 👍/👎.

Автопробуждение не означает постоянный LLM polling. Триггеры должны быть
event-driven или редким scheduler tick.

## 14. Scheduler и process model

Целевая схема:

```text
persona-web        HTTP/SSE, без тяжёлых loops
persona-worker     durable jobs, sleep/review/indexing
persona-telegram   webhook или long-poll adapter + outbox delivery
persona-llm        локальный PC worker / provider
```

Для небольшого deployment `persona-worker` и `persona-telegram` могут быть одним
процессом, но должны быть отдельны от web lifecycle.

Вместо десятков `asyncio.create_task`:

- единая `job` table;
- atomic claim/lease;
- `scheduled_at`, priority, attempts, timeout;
- concurrency groups (`llm`, `db_write`, `telegram_send`, `maintenance`);
- retry/backoff/dead-letter;
- heartbeat;
- startup recovery;
- immutable execution ledger;
- bounded retention.

Scheduler только создаёт due jobs. Worker исполняет. LLM job не держит DB
write-lock во время inference.

## 15. Tool safety

Разделить инструменты:

- read-only;
- local reversible write;
- external communication;
- destructive/sensitive.

Policy учитывает:

- surface: web/DM/group/cron;
- actor: owner/third party/system;
- session mode;
- explicit approval;
- path/domain/resource allowlist;
- budget и max steps.

В Telegram group по умолчанию доступны только безопасные read tools. Отправка
сообщений от имени владельца, изменение файлов, удаление, платежи и доступ к
секретам требуют DM/web approval.

Каждый tool call получает correlation id, idempotency key и audit event.
Prompt injection из web/OCR/chat не может расширить tool policy.

## 16. Privacy

- локальное хранение по умолчанию;
- шифрование bot token и чувствительного evidence at rest;
- redact secrets до LLM/provider;
- cloud-provider policy по типу данных;
- отдельный opt-in для OCR/audio/group ingestion;
- retention по источнику;
- экспорт/forget по человеку, чату, типу и диапазону дат;
- soft-invalidate для логики + hard purge для privacy request;
- уведомление group participants/описание бота о том, что новые сообщения могут
  анализироваться;
- никогда не смешивать third-party profile с owner profile.

## 17. Observability

Нужны dashboards/metrics:

- latency по стадиям handle_turn;
- model/provider, tokens, cost, cache hits;
- tool success/error/denied;
- queue depth/lease age/retries/dead letters;
- Telegram last update/last delivery/webhook status;
- memory candidates/approved/rejected/rolled back;
- false-memory feedback;
- retrieval hit/usefulness;
- sleep duration, evidence count, mutation count, evaluation delta;
- graph orphan/duplicate/contradiction counts;
- proactive messages, open/read/reply/👍/👎;
- privacy-policy denials.

Каждый run должен иметь correlation id от ingress до model, memory и delivery.
В UI нужен “Почему агент это помнит/сделал?” с evidence links.

## 18. Rollout

### Stage 0 — Lockdown

- owner-only mode;
- revoke non-owner sessions;
- disable signup/subscriber access;
- Telegram owner/chat allowlists;
- fail-closed tests.

Критерий: ни один не-owner principal не получает private HTML/API/tool response.

### Stage 1 — Conversation core

- вынести `ConversationService`;
- web route становится adapter;
- сохранить текущий web UX;
- golden regression на prompt/history/tools.

Критерий: web ответы функционально эквивалентны до Telegram.

### Stage 2 — Telegram MVP

- DM owner-only;
- text + reply + streaming edits;
- `/new`, `/status`, `/stop`;
- shared memory;
- inbox dedupe/outbox retry;
- отдельный always-on process.

Критерий: рестарт в середине ответа не создаёт второй user turn и не теряет
завершённый ответ.

### Stage 3 — Unified channels

- channel bindings;
- web ↔ Telegram link;
- topics/groups;
- voice/photo/document ingestion;
- message origin/edit/delete handling.

Критерий: linked thread имеет одну хронологию и не смешивается с другими.

### Stage 4 — Evidence memory

- provenance schema;
- post-turn proposals;
- pending/approve/reject UI и Telegram commands;
- retrieval только approved memory;
- third-party trust policy.

Критерий: любой injected durable fact объясним источником и откатывается.

### Stage 5 — Durable scheduler + wake

- job/lease/execution ledger;
- отдельный worker;
- reminders/briefing/health triggers;
- quiet hours/anti-spam/outbox.

Критерий: due job исполняется максимум одним worker и не теряется после crash.

### Stage 6 — Sleep v2

- snapshot/cursor;
- staged candidates;
- critique/evaluation/apply/rollback;
- dream report с diff и evidence;
- никаких прямых writes старого pipeline.

Критерий: два запуска на одном snapshot дают один логический batch; failed run
не меняет active memory.

### Stage 7 — Graph v2

- aliases/evidence/temporal revisions;
- rebuildable projection;
- graph consolidation и staged merges.

Критерий: повторная индексация одного evidence не увеличивает strength.

### Stage 8 — Procedural learning

- task outcome events;
- Reflexion только на проверяемом feedback;
- skill proposals;
- approval и versioning;
- per-surface tool policy.

Критерий: агент не может сам расширить свои permissions.

## 19. Тестовая стратегия

### Unit

- identity resolution;
- channel binding;
- source trust;
- memory policy;
- candidate scoring;
- graph normalization;
- wake anti-spam;
- tool policy.

### Contract

- web adapter и Telegram adapter дают одинаковый application command;
- Telegram update parsing/formatting;
- LLM/provider/tool ports;
- migration compatibility.

### Security

- anonymous/non-owner/pro subscriber/admin denied;
- forged Telegram user/chat/update denied;
- group participant cannot invoke tools;
- fail-closed при DB/auth error;
- prompt injection не меняет policy;
- secret redaction.

### Concurrency/recovery

- duplicate update;
- two Telegram pollers;
- two job workers claim one job;
- crash before/after model response;
- crash before/after outbox send;
- sleep lease expiry;
- SQLite busy/IO error.

### Memory quality

Golden corpus:

- explicit owner fact;
- changed preference;
- contradiction;
- quoted third-party statement;
- OCR чужого профиля;
- meeting with multiple speakers;
- sarcasm/negation;
- relative date;
- repeated paraphrase;
- sensitive fact.

Измерять precision durable extraction, contradiction accuracy, provenance
coverage, recall@k и false-injection rate. Для auto-apply precision важнее recall.

### End-to-end acceptance

1. Факт сообщён на сайте, затем корректно recalled в Telegram.
2. Факт сообщён в Telegram topic, доступен на сайте только owner.
3. Другой Telegram user получает deny и не создаёт session/message.
4. Group participant не может заставить агента вызвать mutating tool.
5. Sleep предлагает вывод из owner evidence и показывает цитату.
6. Ошибочный вывод rejected и больше не попадает в prompt.
7. Rollback сна восстанавливает previous projection.
8. После рестарта scheduler/outbox продолжают работу без дублей.

## 20. Приоритет проблем

### P0

- owner-only fail-closed;
- единый `ConversationService` до Telegram;
- Telegram allowlist + dedupe + outbox;
- вынести workers из web startup;
- provenance/staging до усиления автоматического сна.

### P1

- durable job scheduler/leases;
- channel/thread identity;
- post-turn memory review;
- sleep snapshot/rollback/evaluation;
- graph evidence и idempotency;
- observability.

### P2

- Telegram topics/group observer;
- voice/media;
- proactive usefulness learning;
- procedural skill proposals;
- richer graph/community summaries.

## 21. Источники

Первичные/официальные:

- Hermes Agent docs: https://hermes-agent.nousresearch.com/docs/
- Hermes features: https://hermes-agent.nousresearch.com/docs/user-guide/features/overview/
- Hermes architecture: https://hermes-agent.nousresearch.com/docs/developer-guide/architecture
- Hermes persistent memory and background review:
  https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/
- Hermes sessions:
  https://hermes-agent.nousresearch.com/docs/user-guide/sessions/
- Hermes session storage:
  https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage/
- Hermes Telegram:
  https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram
- Hermes cron:
  https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
- Hermes memory providers:
  https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers/
- Hermes Auto Dream open feature request:
  https://github.com/NousResearch/hermes-agent/issues/10771
- Telegram Bot API:
  https://core.telegram.org/bots/api
- Telegram privacy mode/features:
  https://core.telegram.org/bots/features
- Generative Agents:
  https://arxiv.org/abs/2304.03442
- MemGPT:
  https://arxiv.org/abs/2310.08560
- Reflexion:
  https://arxiv.org/abs/2303.11366
- Sleep-time Compute:
  https://arxiv.org/abs/2504.13171

## 22. Решение

Для Persona следует перенять у Hermes не выдуманный “REM-алгоритм”, а
проверяемые системные решения:

- one agent core, many surfaces;
- stable session routing;
- bounded curated memory + searchable full history;
- background review с approval;
- durable isolated scheduled runs;
- strict Telegram allowlists;
- прозрачность действий.

«Сон» Persona стоит сохранить как собственное преимущество, но перевести из
прямого LLM→memory mutation в evidence→proposal→critique→evaluation→revision.
Именно provenance, reversible revisions и measured retrieval quality сделают
этот механизм действительно умным, а не просто генератором красивых ночных
текстов.
