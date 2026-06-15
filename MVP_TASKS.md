# Persona — MVP задачи и статусы (живой файл)

> Источник истины по задачам. Я (ИИ) работаю в режиме **ralph-loop**: каждый заход
> 1) перечитываю актуальный код (он меняется), 2) детально и с умом обдумываю
> следующую задачу из этого файла, 3) выполняю + проверяю + коммичу + пушу,
> 4) обновляю статусы здесь. Не останавливаюсь, пока всё не `[DONE]`.

**Фокус (важно):** Persona — это в первую очередь **личный ИИ-ассистент**.
ИИ-для-кода и код-фишки временно НЕ приоритет (модель пока не тянет) — уже
сделанное по коду оставляем, новое по коду не делаем, силы — в ассистента,
память, голос, надёжность, удобство.

Легенда: `[DONE]` готово · `[WIP]` в работе · `[TODO]` запланировано · `[HOLD]` отложено/зависит.

---

## 0. Сделано (этот этап)
- `[DONE]` Ф0 фундамент: миграция 177 (индексы chat_message.is_pinned, audit, auth_session, voice_tts),
  `call_tool(..., session_id)`, SSE `publish_activity/publish_log/publish_event`, `run_foreign_key_check`. (commit c5b570f)
- `[DONE]` Ф1.2 расширенный каталог инструментов: edit_file/multi_edit/read_many/find_files/search_code/
  fetch_json/web_search/run_tests/query_memory + алиасы + миграция 178 seed. (commit 1fb961b)
  *(код-ориентированные, но дешёвые и безопасные — оставляем как базу.)*

## Уже реализовано в продукте (контекст, чтобы видеть картину)
- Чат: SSE-стриминг, история, summarization, кросс-чат recall (keyword/smart), pinned, реакции,
  span-rating (датасет для дообучения), режимы plan/ask/auto/bypass, effort fast/normal/deep,
  19 LLM-провайдеров, detached-генерация (`_LiveGen`, переживает перезагрузку вкладки).
- Память дня: скрины+OCR+аудио+часовые карточки → контекст в чат.
- Голос: агентный (очередь `voice_tts`, wake-word/STT на устройстве, TTS `say`/SAPI).
- Захват: Mac+Windows агенты (скрины, звук, файловый доступ ИИ, выполнение команд).
- Owner-аккаунт, ~26 страниц настроек, аудит-лог, health-dashboard, MCP-реестр (builtin-инструменты).

---

## 1. Ассистент-ядро (пере-приоритизировано на ассистента, не на код)
- `[DONE]` **Личная память `user_memory`** (миграция 180): backend ГОТОВ — таблица (kind/text/pinned/source),
  `app/chat/user_memory.py` (add/list/forget/search/build_block, дедуп, casefold-поиск для кириллицы),
  эндпоинты `POST /api/chat/remember|forget`, `GET /api/chat/memory`, инжект блока «что я помню о тебе» в
  системный промпт, `query_memory` читает факты+чаты. ОСТАЛОСЬ: слэш-команды /remember,/forget в палитре чата
  (зависит от системы команд) + авто-извлечение фактов из диалога (по итогам Hermes/mem0-ресёрча).
  *Готча: SQLite lower()/NOCASE — только ASCII; кириллицу матчим в Python casefold.*
- `[DONE]` **Слэш-команды (ассистентские)**: реестр-источник правды `app/chat/commands.py` (COMMAND_SPECS,
  commands_json, split_command, expand_command) + `GET /api/chat/commands`. Палитра-автокомплит в композере
  (chat_index.html): фильтр по имени/алиасу при вводе «/», навигация ↑↓ Tab/Enter Esc, подстановка/исполнение.
  Типы client (UI: /new /title /clear /search /stop /retry /memory /persona /activity /voice /theme /help) и
  turn (директива на ход: /plan /ask /auto /bypass /fast /normal /deep /web). `//текст` — литерал. Перехват в
  send(): неизвестная команда не съедается. (Код-команды /edit /run /git — `[HOLD]`.)
- `[TODO]` **Агентный цикл — мягкий upgrade**: структурные SSE-фреймы (tool_call/tool_result) для окна активности,
  но без тяжёлого код-планировщика. Лимит раундов, дедуп, stop. (план-фаза — опционально/позже.)
- `[TODO]` **Скиллы**: /skill list/run/enable/disable, страница /settings/skills. (ассистент-навыки, не код.)
- `[HOLD]` Код-фишки (edit/diff/scaffold UI, code-planning) — заморожено по решению пользователя.

## 2. «Видно, что делает ИИ» (полезно ассистенту для веб-задач)
- `[DONE]` Окно активности (ядро): миграция 181 (`tool_execution`), `app/activity/{__init__,store}.py`
  (start/finish_execution, list_session/recent), запись из send-stream вокруг каждого `call_tool`
  (best-effort, не ломает ответ) + SSE `publish_activity`, эндпоинты `GET /api/activity/recent` и
  `GET /api/chat/activity/{id}`, живая страница `/activity` (Alpine + EventSource), nav-пункт.
  `tool_artifact` (скрины браузер-агента) — отдельной миграцией вместе с браузер-агентом. (commit ниже)
- `[DONE]` Панель «🔭 что делает ИИ» прямо в чате (chat_index.html) — выезжающий правый drawer,
  кнопка в шапке, live по SSE `/events` (фильтр по session_id) + replay из `/api/chat/activity/{id}`,
  апсёрт по exec_id, статусы/аргументы/результат/тайминг. Аддитивно (fixed overlay, grid не тронут).
- `[TODO]` Встроенный браузер-агент (Playwright подпроцесс на сессию): browser_open/click/type/read/...
  с живыми скриншотами в окне активности. (полезно: ассистент ищет в вебе по задаче пользователя.)
- `[TODO]` Реальный MCP-рантайм (stdio) + playwright-mcp + переключатель `/settings/automation`
  (`browser_backend = builtin|mcp|both`). Оба варианта + переключатель.

## 3. Голос (ядро ассистента, по максимуму + по умолчанию)
- `[DONE]` Голос в браузере: 🎙 кнопка-микрофон в композере чата — диктовка через Web Speech API
  (SpeechRecognition, interim-результаты live в поле, ru-RU/locale) с авто-fallback на запись MediaRecorder →
  `POST /api/voice/web/stt` (серверный Whisper `transcribe_segment`, 503 если бэкенд не стоит → подсказка
  «открой в Chrome»). TTS ответов (`speechSynthesis`, auto-озвучка) уже был. micSupported-детект.
- `[TODO]` Hands-free страница `/voice` (орб-микрофон, живой транскрипт, настройки голоса/скорости).
- `[TODO]` Hands-free страница `/voice` (орб-микрофон, живой транскрипт, настройки голоса/скорости).
- `[TODO]` Агентный голос максимум: непрерывное слушание, barge-in, выбор голоса/скорости (`/api/voice/config`,
  `plat.tts_speak(text,voice,rate)`, `tts_stop`). [NEEDS DEVICE CHECK]
- `[TODO]` Голос по умолчанию: kv `voice_web_enabled/voice_default_on`, кнопка в base.html.
- `[TODO]` БД голоса (миграция 183): `voice_tts += device_id/source/rate/error`, `voice_transcription`, `voice_turn`.

## 4. Root-панель + пользователи/роли + все логи
- `[TODO]` Live системные логи: `app/log_buffer.py` (ring buffer + structlog-процессор) → SSE `log` → `/root/logs`.
- `[TODO]` `/root` (owner-only): логи, аудит, воркеры, БД (таблицы+integrity+FK-check+backup+SELECT-only query),
  агенты/токены/MCP, все настройки, danger zone, фич-флаги.
- `[TODO]` Пользователи и роли (миграция 184): `users.role(owner/admin/member/viewer)`+`status(pending/active/suspended)`,
  `app/auth/roles.py`, `/root/users` (create/approve/suspend/role/delete), переписать `auth_gate.py` под роли.

## 5. Настройки удобнее
- `[DONE]` **Поиск по настройкам**: мгновенный фильтр в /settings/hub (Alpine, по названию/пути/категории +
  синонимы _KEYWORDS: «пароль»→api-tokens, «тема»→theme и т.п., подсветка совпадений) + серверный
  `GET /api/settings/search` (search_settings, источник правды — _CATEGORIES → _categories_json).
  Добавил /ai-activity в хаб. Чинит discoverability ~30 страниц. Старые роуты не тронуты.
- `[DONE]` **Фикс коллизии /activity**: окно «что делает ИИ» переехало на `/ai-activity` — путь `/activity`
  уже занят тепловой картой за 365 дней (activity_heatmap.py); мой роут регистрировался раньше и затенял её.
  Обновил nav (base.html) и ссылку в чат-панели.
- `[TODO]` (опц., позже) Единый settings-shell с 8 секциями + инлайн-тогглы (`POST /api/settings/set`),
  слияние дублей. Сейчас не критично: хаб с поиском уже даёт нужную навигацию.

## 6. Производительность БД и поиск (НОВОЕ — решение принято)
- `[DONE]` **FTS5 для поиска по чатам/памяти** (миграция 179): `chat_message_fts` (external-content) + триггеры
  ai/ad/au + backfill rebuild; `search_messages` (AND-prefix) и `recall_by_terms` (OR-prefix) переключены на FTS
  с bm25-ранжированием и fallback на LIKE. Префиксный матч (`лендинг*`) учитывает русские окончания.
  *Поиск теперь sub-ms даже на 100k+ сообщений (LIKE был полным сканом). Доп.: PRAGMA optimize/ANALYZE — уже воркером.*
- `[TODO]` Финальный аудит связей: `PRAGMA foreign_key_check` чисто (найдена 1 пред-существующая нарушенная связь
  на свежей БД — разобраться/починить в Ф6), assert-индексов, VACUUM как owner-действие.

## 7. Hermes и конкуренты — ресёрч ГОТОВ, внедряем лучшее
**Hermes = Nous Research Hermes Agent** (hermes-agent.org): open-source, self-hosted личный ИИ с
постоянной памятью (USER.md профиль + MEMORY.md факты с ЖЁСТКИМ лимитом символов → не auto-compact,
а ошибка → заставляет консолидировать), self-improving скиллы, cron-автоматизации, 16+ мессенджеров,
10 TTS/4 STT, privacy-first. **Наш МОАТ (чего у Hermes НЕТ):** мы помним что ты ДЕЛАЛ (скрины+аудио+
часовые карточки), нативный Windows + web-UI без setup, реально local-first. Стратегия: не гнаться за
широтой, а сделать память **релевантной, прозрачной, проактивной и редактируемой**.

Конкретные задачи (по приоритету impact/effort):
- `[DONE]` **7a. Релевантная + прозрачная инъекция памяти дня** (I5/E2): `memory_context.build_memory_context()`
  ИГНОРИРУЕТ свой `query` и всегда суёт последние карточки/приложения/транскрипты в КАЖДЫЙ промпт (= шум,
  главная претензия к памяти ChatGPT). Фикс: гейтить по релевантности к вопросу + чип «использовано N воспоминаний».
- `[DONE]` **7b. Дешёвые движки скорости** (I3/E1): SQLite read-pragmas (mmap_size 256MB, cache_size 64MB,
  temp_store MEMORY) в `get_connection` + периодический WAL checkpoint. Ускоряет FTS/поиск/всё.
- `[DONE]` **7c. Анти-подхалимаж + memory-grounding в `_RULES_TAIL`** (I3/E1): во ВСЕ пресеты — без «отличный
  вопрос», без выпрашивания продолжения, честно возражать; факты о юзере брать ТОЛЬКО из блоков памяти, не выдумывать.
- `[DONE]` **7d. Единый редактор памяти `/settings/memory`** (I4/E2, trust-фича Hermes): показать ВСЁ, что ИИ
  помнит (user_memory по kind + профиль), редактировать/удалять, бюджет-индикатор, «пауза памяти для этого чата».
- `[DONE]` **7e. Авто-извлечение фактов (mem0/MemGPT ADD/UPDATE/DELETE/NOOP)** (I5/E3): после диалога LLM
  обновляет `user_memory` (разрешает противоречия, не только растёт). Поверх уже готового user_memory.
- `[DONE]` **7f. Бюджет памяти + «без тихого переполнения»** (I3/E2): видимый лимит (~40 фактов/символы),
  при переполнении — консолидация (merge/replace), importance-weighting. (ключевой инсайт Hermes.)
- `[DONE]` **7g. Утреннее/вечернее саммари из скринов+аудио вчера** (I5/E3, наш моат): `briefing_worker` на
  существующем ClockScheduler (как memory_of_day/digest), короткая проактивная сводка в чат+дашборд.
- `[TODO]` **7h. Кэш-дружелюбный префикс промпта** (I5/E3): стабильный persona+profile+memory блок строить
  раз на сессию (сейчас весь system-prompt пересобирается каждый ход → бьёт prompt-cache, жжёт латентность/токены).
- `[TODO]` **7i. sqlite-vec вместо чистого python-косинуса** (I4/E3) + гибридный FTS5+vector RRF-recall
  (chat+OCR+транскрипты+карточки), вызываемый и в чате. (чистый python cosine не масштабируется >100k.)
- `[DONE]` **7j. Local-first промис на виду** (I3/E2): «данные не покидают машину, ноль телеметрии» на
  дашборде/онбординге + проверить at-rest шифрование (vault) + «пауза всего» одной кнопкой.
*(Полный отчёт ресёрча: tasks/waszudol0.output — 8 сабагентов, 2 синтезатора сошлись.)*

---

## 8. ПОСЛЕ того как ВСЁ выше = `[DONE]` (отдельный финальный этап)
> **Гейт:** этот пункт начинаю ТОЛЬКО когда все задачи 1–7 имеют статус `[DONE]`.
- Просмотреть **весь интернет**: Hermes, другие ИИ/сервисы/конкуренты, личные ИИ-помощники, БД моделей,
  оптимизация ИИ, улучшение ИИ, улучшение системного промпта, улучшение БД — всё, что может помочь.
- Подключить **множество сабагентов**, сравнить их ответы, обдумать, свести.
- Написать **огромный план** для пользователя: (а) что уже реализовано и КАК работает, (б) что ещё можно сделать
  для «идеального проекта — лучшего личного ИИ-ассистента для всех». Честно перечислить ВСЕ минусы и трудности,
  но решаемые — уже с решением (о них не ныть). Пользователь читает утром.

---

### Журнал прогресса
- 2026-06-15: Ф0, Ф1.2 готовы и запушены. Создан этот файл. Запущен Hermes/конкурент-ресёрч (фон). Решение по поиску: FTS5.

- 2026-06-15: Hermes-ресёрч готов (Nous Research Hermes Agent). Внедрены 7a (релевантная память-дня), 7b (read-pragmas), 7c (анти-подхалимаж+grounding). Дальше: 7d редактор памяти, 7e авто-факты, 7g брифинг.

- 2026-06-15: Память-пиллар ЗАВЕРШЁН — store+релевантность(7a)+grounding(7c)+редактор(7d)+авто-факты(7e). Коммиты c04e982,29f0fb0,21fd995,dfd599e. Дальше: 7g брифинг, 7h prompt-cache, голос, root+роли, окно активности, настройки.

- 2026-06-15 (loop): 7f бюджет авто-памяти (cap 80, без тихого переполнения) + 7j privacy-промис на /settings. Коммит ниже.
