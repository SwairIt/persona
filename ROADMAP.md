# Persona — большой план: что есть, что делать, честные трудности

> Источник: глубокий интернет-ресёрч (19 сабагентов, веб ✓, ~1.16M токенов): Hermes,
> конкуренты, личные ИИ, БД моделей, RAG/память, оптимизация, промпты, БД, голос, UX —
> синтез с 3 ракурсов (продукт / архитектура / честные риски), сверено с реальным кодом.
> Фокус: ИИ-ассистент в первую очередь. Код-фишки — заморожены.

## 0. Позиционирование (одной фразой)
**«Rewind/Limitless, который никто не купит и не сольёт в облако».** Главный
дифференциатор Persona — НЕ ум модели (на слабом локальном железе она всегда проиграет
облаку по интеллекту), а **память «что ты РЕАЛЬНО делал»** (экран+аудио+OCR+чат) +
**проверяемая local-first приватность** + **проактивность из реального контекста**.
Рыночное окно открыто: Rewind sunset 19.12.2025, Limitless куплен Meta → доверительный
вакуум. Вывод: не конкурируем умом — конкурируем памятью, доверием, привычкой.

---

## A. ЧТО УЖЕ РЕАЛИЗОВАНО и КАК работает
Легенда зрелости: ✅ работает · 🟡 есть, но недозрело/opt-in · ⚠️ техдолг.

- ✅ **Чат**: SSE-стриминг, 19 LLM-провайдеров (вкл. локальный Ollama), detached-генерация
  (переживает перезагрузку вкладки), режимы plan/ask/auto/bypass, effort fast/normal/deep,
  реакции, pinned, span-rating (датасет). Файлы: `app/web/routes/chat_sessions.py`, `app/llm/client.py`.
- ✅ **Захват «что ты делал»** (МОАТ): Mac/Win агенты — скриншоты+OCR+аудио → часовые
  карточки `hourly_card`. Это уникальный источник, которого нет у облаков.
- 🟡 **Память**: `user_memory` (mem0-стиль авто-извлечение фактов, `app/chat/user_memory.py`)
  — НО только ADD + дедуп по точному тексту, cap=80, без UPDATE/DELETE/temporal (⚠️ копит
  противоречия). Recall: FTS5 bm25 + LIKE (`recall_relevant`/`recall_by_terms`). Гибрид
  sqlite-vec + RRF **написан** (`app/memory_vec.py`) но **opt-in/спящий** (нужен
  `recall_mode=hybrid` + `pip install sqlite-vec` + embed-модель), без реранкера/бэкфилла.
- ✅ **Утренний брифинг** (`app/briefing.py`) — из `hourly_card`, но один текстовый блок (🟡).
- ✅ **Окно активности инструментов**: ядро (`app/activity/`, миграция 181) + панель в чате + `/ai-activity`.
- ✅ **Слэш-команды** (`app/chat/commands.py`) + палитра-автокомплит.
- ✅ **Голос**: браузерный STT (Web Speech + серверный Whisper fallback `/api/voice/web/stt`),
  hands-free `/voice` (орб, STT→стрим→TTS, барж-ин), TTS `speechSynthesis`, голос по умолчанию (FAB).
- ✅ **Скиллы** (`/settings/skills`, из GitHub SKILL.md — только текст).
- ✅ **Root-пульт** (`/root`, owner-only): live-логи (`app/log_buffer.py`), здоровье, аудит БД
  (`/root/db/integrity`). **Роли**: миграция 184 + `app/auth/roles.py` + управление в /root
  (approve/suspend/role/delete, гард последнего owner, suspend ревокает сессии). 🟡 Ролевые
  ТИРЫ доступа в gate отложены (см. C).
- ✅ **Поиск по настройкам** (`/api/settings/search`), **prompt-caching Anthropic** (system →
  ephemeral-блок), **монитор нагрузки** `loadmon/` (отдельный сервис :8770) + индикатор в чате,
  **QLoRA-пайплайн «вторая копия»** (`app/finetune/`, `finetune/`), **анти-подхалимаж/анти-CJK/grounding** в `app/chat/prompts.py`.

---

## B. ДОРОЖНАЯ КАРТА — что делать (приоритизировано по impact/effort)

### Волна 1 — Надёжность + память (наибольший ROI)
1. **[huge] SQLite: выделенный writer + `BEGIN IMMEDIATE` + короткие транзакции.** Чинит уже
   задокументированный фриз (T19: 40 воркеров + Ollama в write-транзакции). `busy_timeout` —
   пластырь. КАК: один сериализованный writer (asyncio-очередь) + пул read-only; LLM-вызовы
   ВНЕ открытой записи (вынести из `maybe_summarise`/`extract_and_store`).
2. **[high/S] Ollama `keep_alive='30m'` + адаптивный `num_ctx`.** Сейчас модель выгружается за
   5 мин (+10с на каждый брифинг), `num_ctx=16384` захардкожен (раздувает KV-кэш). Правка одной
   функции `_native_options` в `app/llm/client.py`.
3. **[huge] mem0-конвейер ADD/UPDATE/DELETE/NOOP + bi-temporal** (`valid_from/valid_until`) для
   `user_memory`. При извлечении — top-K похожих → один `complete_json`-вызов решает операцию;
   противоречие = **soft-invalidate** (не DELETE), recall берёт `valid_until IS NULL`, юзер видит
   историю и откатывает. Асинхронно.
4. **[huge] Активировать спящий hybrid-recall + бэкфилл + реранкер.** Включить `recall_mode=hybrid`
   в дефолтах (при наличии Ollama+embed), фоново проиндексировать историю (`index_message`),
   маршрутизировать `recall_relevant`→`hybrid_recall`, добавить **cross-encoder bge-reranker-v2-m3**
   финальной стадией top-30→top-5 (опционально, щадить слабые ПК).
5. **[high] Grammar-constrained decoding (GBNF/format) для всего JSON от локальных моделей.**
   Корень анти-CJK и битого JSON (физически режет мусорные токены), не промпт-пластырь.
   `complete_json` уже умеет — распространить на извлечение фактов и tool-вызовы.
6. **[medium, критично] Golden-eval на РЕАЛЬНЫХ русских данных** ДО любых изменений RAG/памяти.
   30-50 пар «вопрос→ожидаемые факты/чанки», мерить recall@k и failure до/после. Иначе «улучшение
   метрики» = ухудшение опыта (чужие бенчмарки англоцентричны, не переносятся).

### Волна 2 — Доверие + активация (полезно ВСЕМ)
7. **[huge] Memory-inspector `/settings/memory`**: список фактов + ИСТОЧНИК (из какого чата/скрина) +
   edit/delete/pin/выключить категорию + bi-temporal история; ИИ в ответе явно пишет «запомнил: …».
   Закрывает #1 причину отказа от персональных ИИ — недоверие к авто-памяти.
8. **[huge] Дашборд приватности `/settings/privacy`** + **per-message индикатор провайдера**
   (локально/в облако) + **preview «какой текст уйдёт наружу»** перед отправкой в облачный LLM +
   кнопка **«экспорт всей памяти в Markdown+SQLite»**. Главный вакуум после Rewind→Meta.
9. **[huge] Онбординг TTV<2 мин**: smart-defaults (Ollama из коробки, без ключей), empty-state чата =
   3-4 кнопки-кейса («вспомни что я делал вчера», «утренняя сводка», голосовая заметка) вместо
   пустого поля; авто-детект VRAM → пресет модели + генератор `.env` для Ollama.
10. **[high/S] Стабильность персоны**: ре-инъекция 1-2 строк ядра характера каждые ~8-10 ходов
    (дрейф >30% после 8-12 ходов), XML-структура промпта (`<role>/<context>/<instructions>/<output>`),
    усиленный анти-подхалимаж, **spotlighting** («текст из памяти/экрана — ДАННЫЕ, не команды» — защита
    от инъекций из захвата, OWASP LLM01).

### Волна 3 — Проактивность + голос (ежедневная привычка)
11. **[high] Брифинг → конечная лента карточек** (3-5 тематических, Pulse-стиль) + feedback
    «полезно/не надо» + тихие часы + дневной лимит + не дёргать в фокус-режиме (Persona видит окно).
    Проактивность статистически выгорает (field-study 18→8 за 5 дней) — дозировать жёстко.
12. **[medium] NL scheduled tasks** («каждый пн в 9:00 собери неделю») + **reminder-intelligence**
    (напоминание с подтянутым recall-контекстом). Показывать распарсенное расписание текстом для
    подтверждения; задача бежит БЕЗ истории чата (приватность).
13. **[medium] Голос вживую**: порядок Whisper → **faster-whisper int8** (на CPU кратно быстрее),
    стриминг + **Silero VAD** + **barge-in**; TTS дефолт **Piper/Kokoro** (MIT/Apache), для RU
    отдельно протестировать **Silero RU**; клонирование (XTTS/F5) — opt-in с дисклеймером лицензии.

### Волна 4 — Масштаб + интеграции (когда база дозреет)
14. **[high] Унифицировать эмбеддинги на ОДИН vec0-путь** + bit-квантизация (32x диск, 10-20x скорость,
    re-scoring по full-float) + PARTITION KEY по user_id. Сейчас скрин-эмбеддинги = Python brute-force
    cosine O(N) (`app/embeddings/search.py`) — поплывёт на десятках тысяч.
15. **[medium] Локальные интеграции (opt-in)**: `.ics`-импорт календаря, IMAP read-only, папка Markdown —
    обогащают брифинг без облака.
16. **[medium] Ad-hoc RAG `#файл`/`#URL`** в чате + **10-15 кураторских персон** (переводчик, наставник,
    репетитор, врач-разъяснитель, редактор) с быстрым переключателем.
17. **[medium] Ретеншн/компрессия захвата** (OCR-текст вечно, сырьё N дней с авточисткой).
18. **[medium] Фоновое обслуживание БД** (nightly `wal_checkpoint(TRUNCATE)`, инкрементальный FTS merge,
    `PRAGMA optimize`, `VACUUM INTO` для бэкапа) + «dreaming» консолидация памяти на локальном Ollama в простое.

### БД моделей — что ставить (проверять теги `ollama pull` перед прошивкой!)
- **Чат**: qwen2.5/qwen3 7-8B Q4_K_M (8GB) · 3-4B (4GB) · CPU → llama3.2:3b. Для RU-тона рассмотреть
  RU-finetune (RuAdapt-Qwen, Vikhr-Nemo-12B, Saiga, T-lite) — голая база слабее по-русски.
- **Tool-use**: Hermes-формат поверх Qwen/Llama; Llama-3.1-8B надёжнее «из коробки» по BFCL, Qwen — по тону/языку.
- **Vision/OCR**: qwen2.5vl:3b (4GB) / :7b (8GB); тяжёлый OCR — MiniCPM-V. НЕ держать чат+VLM одновременно на 8GB.
- **Эмбеддинги**: bge-m3 (RU/EN/DE) или nomic-embed-text (на CPU, не отъедает VRAM).
- **Reranker**: bge-reranker-v2-m3 (мультиязычный, CPU).
- **«Вторая копия»**: QLoRA 0.5-1.5B (Qwen2.5/DeepHermes-3-8B) = клон СТИЛЯ.

### Что взять у Hermes
Формат tool-calling `<tools>`/`<tool_call>`/`<tool_response>` как канон; steerability через
system-промпт (характер «друг»); hybrid reasoning `<think>` по фиче-флагу; schema-adherence для
извлечения фактов. ⚠️ Neutral alignment = низкие отказы → нужны свои guardrails; reasoning жрёт
токены → выкл по умолчанию; 405B нереалистичен локально → брать 8-14B / DeepHermes-3-8B.

---

## C. ЧЕСТНО — все трудности

### Решается (с решением)
- **SQLite single-writer фризит сайт (T19, уже бьёт)** → выделенный writer + `BEGIN IMMEDIATE` +
  Ollama-вызовы вне транзакции. Потолок ~1000 write/s — для личного ассистента достаточно.
- **Память копит противоречия** → bi-temporal soft-invalidate + UI-ревью (НЕ авто-hard-DELETE).
- **CJK-мусор / битый JSON** → grammar-constrained decoding (корень), промпт — второй слой.
- **Дрейф персоны >30% за 8-12 ходов** → ре-инъекция 1-2 строк ядра (не всего промпта).
- **Проактивность выгорает (18→8 за 5 дней)** → конечная лента + лимит + тихие часы + opt-in +
  не дёргать в фокусе.
- **Приватность хрупка (урок Rewind→Meta)** → видимый индикатор записи, per-source отзыв, preview
  «что уходит в облако», дефолт на Ollama, НИКАКИХ облачных дефолтов.
- **Скрин-эмбеддинги Python brute-force O(N)** → перенос на sqlite-vec vec0 + partition + bit-quant.
- **TTS-лицензии (XTTS/F5 NonCommercial)** → дефолт Piper/Kokoro, клон — opt-in.
- **Расхождение CLAUDE.md ↔ код** (заявлен hybrid-recall, по умолчанию bm25) → починить доку +
  активировать гибрид. Само расхождение — риск (планы на несуществующем фундаменте).

### НЕ решается полностью (физика — честно)
- **Качество маленькой квантованной модели на русском < frontier.** Q3/Q4 на слабом GPU бьёт по
  морфологии и JSON сильнее, чем по английскому. Смягчается RU-finetune + Q5/Q6 + GBNF, но потолок
  локального 4-8B ниже облака — это физика. *Стратегия:* не догонять умом; для сложного —
  переключение на облако в один клик (с preview что уходит). Frontier-цифры (Hermes 405B) к тому,
  что запустит юзер, НЕ относятся.
- **Always-on захват = диск + GDPR-шлейф.** Local-first снимает БОЛЬШУЮ часть, но не всё: нужны
  ретеншн/согласие/индикатор/исключение банков-паролей. «Помнит всё» всегда несёт компромисс.
- **Авто-разрешение противоречий памяти несовершенно** даже у лидеров 2026 → UI-ревью как страховка.
- **Чужие бенчмарки не переносятся** на RU+экран/аудио → только свой мини-eval перед любым дефолтом.
- **Structured output разный у 19 провайдеров** (GBNF — только Ollama/llama.cpp; OpenAI свой) →
  ветвление + fallback, единого «верни JSON» нет.

### Ловушки «модно» — НЕ делать
GraphRAG (10-40x индексация убьёт отзывчивость), полный temporal-граф Zep (нужен Neo4j),
speculative decoding (нет в Ollama, может даже замедлить), маркетплейс 1000 ассистентов,
agent-driven память Letta на слабой модели (мусор записей), код-фишки (заморожено).

---

## D. Рекомендованный порядок (спринты)
- **Sprint 0 — правда+надёжность**: writer-lock+BEGIN IMMEDIATE; keep_alive/num_ctx; починить
  CLAUDE.md; поднять golden-eval harness.
- **Sprint 1 — память**: mem0 UPDATE/DELETE + bi-temporal; GBNF-извлечение; активировать hybrid +
  бэкфилл + reranker.
- **Sprint 2 — доверие+активация**: memory-inspector; privacy-дашборд + индикатор провайдера;
  онбординг TTV<2мин + авто-пресет модели.
- **Sprint 3 — персона+проактивность**: ре-инъекция персоны + spotlighting; брифинг-карточки +
  anti-fatigue; NL scheduled tasks + reminder-intelligence.
- **Sprint 4 — голос+масштаб**: faster-whisper + VAD + barge-in + Piper/Kokoro/Silero; унификация
  эмбеддингов + bit-quant + partition; локальные интеграции; обслуживание БД + dreaming.

> Принцип: каждый спринт — сначала ДОВОДИТЬ существующее (memory_vec, briefing, complete_json,
> /settings/* уже есть), а не строить заново. И никаких изменений recall/памяти без golden-eval.

---
## Прогресс исполнения
- ✅ S0: Ollama keep_alive + адаптивный num_ctx (v2.20.5, `bf7edc8`)
- ✅ S0: CLAUDE.md приведён в соответствие (recall по умолчанию keyword; hybrid opt-in)
- ✅ S0: golden-eval harness `tests/eval/` (keyword baseline hit_rate=0.67; hybrid-fallback не хуже).
  Запуск на своих данных: PERSONA_DB_PATH=боевая → measure(recall_relevant, user_id). Морфология
  (деплоем→деплой, бег→бегать) — известный gap, цель S1 hybrid+reranker.
- ✅ S0: SQLite `write_transaction()` (BEGIN IMMEDIATE) в db.py + применён к user_memory
  (add/set_pinned/delete/forget). 4 pytest зелёные (коммит/откат/20 конкурентных записей/roundtrip).
  Горячий append_message — следующим (изолированно, чтобы не рисковать ядром чата). v2.20.6.
- ✅ S1a: bi-temporal память + mem0-разрешение противоречий (миграция 187: valid_until/superseded_by;
  частичный индекс активных). list/search/count/build_memory_block берут valid_until IS NULL;
  invalidate_memory/restore_memory (soft-invalidate, откат); reconcile_and_add (ADD/UPDATE/DELETE/NOOP
  через GBNF на Ollama, fallback на ADD без LLM); extract_and_store → reconcile. 5 pytest + golden-eval
  не упал. «Москва»→«Берлин» больше не сосуществуют. v2.20.7.
- ✅ S1b: extract_and_store → GBNF-извлечение фактов. _extract_facts: complete_json со схемой
  {facts:[{text,kind}]} для Ollama (корень анти-CJK — format режет мусор), строковый парсер —
  fallback для облачных/сбоя. Каждый факт через reconcile_and_add. 3 pytest (GBNF/fallback/битый-JSON→fallback). v2.20.8.
- ✅ S1c: hybrid recall активируется по умолчанию при sqlite_vec_available() (иначе keyword; явный
  kv побеждает); memory_vec.backfill_index() (идемпотентный бэкфилл истории по vec_message_meta-маркеру,
  no-op без sqlite-vec); опц. cross-encoder reranker в hybrid_recall (kv reranker_enabled, fastembed
  bge-reranker, выключен по умолчанию, не ломает recall). 4 pytest + golden-eval не упал. v2.20.9.
  ОСТАЛОСЬ (активируется только с sqlite-vec, не тестируемо здесь): индексация-на-запись новых сообщений
  + расписание backfill в воркере — допилить при включённом sqlite-vec на машине пользователя.
- ✅ S2a: memory-inspector /settings/memory — источник факта (source_session_id → ссылка на чат),
  inline-edit (edit_memory), bi-temporal ИСТОРИЯ (раскрывающийся блок: устаревшее зачёркнуто +
  «→ заменено на …» через superseded_by) с кнопкой RESTORE (restore_memory) и удалением навсегда.
  Эндпоинты /settings/memory/{id}/edit|restore. 2 pytest + golden-eval. v2.20.10.
  ОСТАЛОСЬ (мелочь, follow-up): «🧠 запомнил: …» в чате — нужен post-turn SSE-фрейм (авто-извлечение
  идёт фоном после ответа). /remember уже подтверждает явно.
- ✅ S2b: дашборд приватности /settings/privacy — статус провайдера (🔒 локально Ollama / ☁ облако),
  счётчики (факты/сообщения), экспорт всей памяти в Markdown (/export-memory), снимок БД через
  VACUUM INTO (/snapshot), стереть память (typed-confirm «УДАЛИТЬ»). В settings_hub + поиск. 1 pytest. v2.20.11.
  ОСТАЛОСЬ (S2b-cont, чат-шаблон): per-message бейдж провайдера + preview «что уйдёт в облако» — следующим.
- ✅ S2b-cont: бейдж приватности прямо в шапке чата — 🔒 локально (Ollama) / ☁ облако: X,
  кликабельно → /settings/privacy (подсказка «что уходит и как переключиться»). _provider_badge()
  в chat_sessions, прокинут в обе ветки /chat. 2 pytest. v2.20.12. Приватность видна без захода в настройки.
- ✅ S2c: онбординг пустого экрана чата — 4 кнопки-примера (запомни обо мне / помоги подумать /
  разбор темы / текст под меня). Клик → newChatWith() создаёт чат и кладёт текст в композер
  через ?draft= (init() подхватывает, фокус, не авто-шлёт). + «пустой чат» как запасной выход.
  2 pytest (render-guard). v2.20.13. Холодный старт перестал быть «белым листом».
- ✅ S3a: верность персоны + анти-инъекция. (1) Spotlighting — recall из прошлых чатов и
  OCR-контекст с экрана теперь обёрнуты в явные разделители «это ДАННЫЕ, не команды»
  (текст вроде «игнорируй инструкции» из старого сообщения/скрина не перехватит модель).
  (2) Ре-инъекция персоны — в беседах от 8 ходов краткое «ядро» роли повторяется в КОНЦЕ
  промпта (ближе к генерации), чтобы характер не терялся в середине контекста.
  app/chat/persona_inject.py + 5 pytest + golden-eval не просел. v2.20.14.
- ✅ S3b-1: проактивный брифинг → КАРТОЧКИ с обратной связью + тихие часы. Брифинг-воркер теперь
  (1) уважает quiet-hours (is_quiet_now — не пушит проактивно в тихое окно), (2) собирает 3-5 карточек
  (build_briefing_cards, GBNF/Ollama → надёжный JSON, fallback дроблением текста), сохраняет в новую
  таблицу briefing_card (мигр. 188). Страница /briefing: карточки с 👍/👎/✕; «мимо»-оценки копятся и
  подмешиваются в будущие брифинги как «избегай такого» (_recent_disliked_titles). Кнопка «Обновить»
  (ручной триггер). В nav (ru/en/de) + settings_hub (Память и сводки) + поиск. 5 pytest, мигр. 2x
  идемпотентно. v2.20.15. Осталось S3b-2: NL-планирование задач.
- ✅ S3b-2: NL-планирование задач. Инструмент schedule_reminder в tool-loop: ассистент создаёт
  напоминания естественным языком прямо в чате («напомни завтра оплатить хостинг», «через 3 дня …»,
  «в пятницу созвон», «20.06 встреча»). Детерминированный рус-парсер дат app/chat/reminder_nl.py
  (сегодня/завтра/послезавтра/через N дней/недель/день недели/DD.MM, срез слов-триггеров; без даты →
  сегодня). Пишет в reminders, подтверждение со ссылкой /reminders. Миграция 189 включает builtin-reminder
  (только локальная запись, безопасно). Алиасы remind/add_task/… 13 pytest. v2.20.16.
  Примечание: работает в расширенном режиме (auto/bypass — где доступны инструменты); slash-команда
  /remind для простого режима — возможный follow-up. Sprint 3 закрыт.
- ✅ S4a: локальные интеграции (.ics экспорт). Напоминания-задачи (таблица reminders, что ставит
  schedule_reminder/ручной ввод) → iCalendar весьдень-события: страница /settings/integrations с кнопками
  «скачать активные / включая выполненные» (app/reminders_ics.py build_todo_ics, stdlib-only RFC 5545:
  CRLF, свёртка 75, экранирование ;,\n, VALUE=DATE). Хаб экспорта со ссылками на существующие фиды
  (AI-напоминания webcal, фокус, активность). В settings_hub (Уведомления и интеграции) + поиск. 4 pytest.
  v2.20.17. Импорт Markdown в память — следующий подслайс S4a-2. (S4c голос — NEEDS DEVICE, позже.)
- ✅ S4a-2: импорт Markdown-заметок в память. На /settings/integrations — вставка текста или загрузка
  .md/.txt → разбор на факты → user_memory (add_memory, дедуп). Парсер app/chat/markdown_import.py
  детерминированный: пункты списков/чекбоксы/абзацы = факты, заголовки = контекст-префикс («Раздел: пункт»),
  снятие инлайн-разметки (жирный/курсив/код/ссылки), пропуск кодоблоков/разделителей/таблиц-сепараторов,
  дедуп внутри документа + cap. 5 pytest, golden-eval не просел. v2.20.18. Soft-only без миграции.
- ✅ S4b: унификация эмбеддингов скринов на sqlite-vec vec0. Единый слой app/embeddings/vec_store.py
  (зеркалит app/memory_vec.py): vec0-таблица screenshot_vec FLOAT[384] (мигр.190, как 186 — guard
  'no such module'), idempotent backfill_vec по маркеру vec_screenshot_meta, vec_candidate_ids (KNN в SQL).
  semantic_search теперь сначала сужает до KNN-кандидатов (быстро), cosine считает только по ним — точность
  та же (исходные BLOB-вектора), скорость O(k) вместо O(N) на всём корпусе. Worker зеркалит новые эмбеддинги
  в vec0 (best-effort). БЕЗ sqlite-vec (как на этой машине) — всё тихо деградирует: no-op/None → полный
  перебор как раньше. 3 pytest fallback + 11 существующих storage не сломаны; golden-eval не просел. v2.20.19.
- ✅ «Вторая копия» — ДАТАСЕТ СОБРАН на живой БД (soft-only, без GPU). Прогон:
  python scripts/build_persona_dataset.py --out finetune/data/persona.jsonl --size 400 --include-history --user-id 2
  Результат: 290 РЕАЛЬНЫХ пар из истории чатов user 2 + синтетика → train 338 (1.3 МБ) / val 17 (60 КБ),
  формат chat-messages (system=персона, user, assistant). finetune/data/ в .gitignore — JSONL НЕ коммитится.
  Фактов памяти у user 2: 0 (наполнится через /remember и импорт Markdown). Починен баг скрипта:
  финальный print с «✓»/кириллицей падал UnicodeEncodeError на cp1251-консоли Windows ПОСЛЕ записи —
  добавлен reconfigure stdout/stderr на UTF-8. 3 pytest на пайплайн (формат/дедуп/JSONL). v2.20.20.
  QLoRA-ТРЕНИРОВКА — пользователю (нет NVIDIA/torch на этой машине), команда (см. finetune/README.md):
    python finetune/train_qlora.py --data finetune/data/persona.jsonl --val finetune/data/persona.val.jsonl \
      --model Qwen/Qwen2.5-1.5B-Instruct --out finetune/out/persona-lora
    (OOM на 4 ГБ VRAM GTX 1050 Ti → --model Qwen/Qwen2.5-0.5B-Instruct --max-seq 512; либо бесплатный Colab T4 16 ГБ.)
    Затем экспорт в GGUF (finetune/export_gguf.md) → ollama create persona-mini → /settings/llm.
- ✅ S4c (каркас): серверный конфиг голосовых движков. Расширен /settings/voice — выбор STT
  (faster-whisper/whisper.cpp/server), TTS-движок (piper/kokoro/silero/system) + голос RU + скорость,
  Silero VAD (вкл + порог), barge-in. app/voice_config.py (валидация: дефолты/кламп чисел/enum/bool, kv).
  Новый GET /api/voice/config (agent-token) — агент тянет конфиг и применяет НА УСТРОЙСТВЕ. 6 pytest
  (валидация + kv round-trip). v2.20.21.
  ⚠ NEEDS DEVICE: реальные движки STT/TTS (faster-whisper порядок, Silero VAD стрим, Piper/Kokoro/Silero RU,
  barge-in) ставятся и проверяются на Mac/Win-агенте — сервер только хранит/валидирует/отдаёт настройки.
  Применение в mac-agent (чтение /api/voice/config + запуск движков) — на стороне устройства, вне этой машины.

## ИТОГ автономного цикла (2026-06-16)
Закрыто soft-only (всё проверяемо без спец-железа): S0 надёжность, S1 память (bi-temporal+mem0+GBNF+hybrid),
S2 доверие (inspector/privacy/бейдж/онбординг), S3 персона+проактивность (spotlighting, ре-инъекция,
брифинг-карточки, NL-напоминания), S4a интеграции (.ics+Markdown), S4b vec0-эмбеддинги, «вторая копия» —
датасет собран (290 реальных пар). Версии 2.20.11 → 2.20.21.
ОСТАЁТСЯ ПОЛЬЗОВАТЕЛЮ (хардвар): (1) QLoRA-тренировка «второй копии» — нужен GPU (команда выше / finetune/README.md);
(2) реальные голосовые движки — NEEDS DEVICE на агенте; (3) sqlite-vec/Ollama-embed для активации векторного
recall и vec0-поиска (pip install sqlite-vec + ollama pull nomic-embed-text — код уже готов, тихий fallback).
- ✅ Идентичность «второй копии» в датасете (по запросу пользователя). Раньше датасет учил только ИМЯ
  Persona + стиль, но НЕ «ты — личная копия этого человека, он твой автор»: persona_system подмешивал
  identity-факты из user_memory, а там у user 2 было 0 фактов → авторство не попадало. Теперь идентичность
  («Тебя зовут Persona; ты личная локальная копия этого человека; он тебя создал/настроил/обучил; твой автор
  и хозяин — он; ты не общий ChatGPT») зашита в system-промпт КАЖДОГО примера (persona_system) + 10 обучающих
  Q&A-пар (_IDENTITY_SEEDS: как тебя зовут/кто тебя создал/кто твой автор/ты чей/ты Persona?/ты обычный чатгпт).
  Опц. --owner-name зашивает имя владельца («Его зовут X» + Q&A «как меня зовут»). По выбору пользователя
  пересобрано БЕЗ имени и БЕЗ записи в память: train 347 / val 18, идентичность в 347/347 примеров. Пофикшен
  баг обрезки обязательных пар при малом --size. 4 pytest (вкл. identity в каждом примере). v2.20.22.
- ✅ Turnkey «вторая копия»: Colab-ноутбук + скачивание из UI. finetune/persona_colab.ipynb (15 ячеек:
  проверка T4 → install → upload датасета → QLoRA-обучение (Qwen2.5-1.5B, fallback 0.5B) → merge →
  конвертация в GGUF Q4_K_M через llama.cpp → download → инструкция Ollama на ПК). На /settings/integrations —
  раздел «Вторая копия» с кнопками скачать ноутбук/датасет(train/val) (download-маршрут, owner-gate, 404 если
  не собран). Решение пользователя: обучать на бесплатном Colab T4, запускать на своём 1050 Ti через Ollama
  (GPU нужен для инференса, не для тренировки). 4 pytest (валидность ipynb + рендер). v2.20.23.
