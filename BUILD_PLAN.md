# PERSONA — Большой план доработки (BUILD_PLAN)

> Источник истины автономного исполнения. Каждый тик: взять следующий невыполненный пункт →
> построить ОДИН проверенный срез → verify (import/jinja/миграции 2× идемпотентно/pytest) →
> бамп `app/__init__.py` + `sw.js` → commit+push master (токен маскировать) → отметить `[x]` здесь.
> Старт: v2.20.27, HEAD 6723e8a, 106 892 строк кода (py/html/js/sql), 2026-06-16 23:30.

## Принцип (важно)
Проект УЖЕ огромный (395+ страниц/эндпоинтов). НЕ строим с нуля — **унифицируем существующее +
связываем навигацией + полируем**. Reuse: `_day_bounds_utc` (day_json.py), `_group_by_hour`
(timeline.py), `.glass/.grad/.reveal/bento/.stat/.chip` (landing/style.css), cal_nav.js, FTS
(`hourly_card_fts`, `_fts_expr`), `llm_usage`/`tool_execution`/`audio_segment` для статистики дня.

---

## ФАЗА A — Кабинет: ЕДИНАЯ страница ДНЯ + сквозная навигация (ЦЕНТР, делаем первым)
Цель пользователя: клик по вершине графа → страница этого дня со всеми скринами, можно сразу
спросить про день, видно был ли захват, сколько использовался ИИ и т.д. И быстрый переход туда без графа.

- [x] A1. `app/day_overview.py` — агрегатор дня (reuse `_day_bounds_utc`): за дату вернуть
  screenshot_count + разбивка ocr_status, audio_seconds (=«был ли/сколько записан звук»),
  hourly_card list, chat msgs + input/output токены, tool_execution разбивка, llm_usage по kind,
  voice_tts done, day_tldr, daily_budget_state. Один объект DayOverview. + pytest на scratch-БД.
- [x] A2. Роут+шаблон `/day/{date}` (`day_overview.py` route, `day_overview.html`): шапка (дата,
  ← →, «сегодня», TL;DR), KPI-плитки (скрины / звук N мин / ИИ: вызовов+токенов / активных часов),
  скрины по часам (reuse timeline-группировку, ссылки на /screenshot/{id}), часовые карточки,
  быстрые ссылки на существующие виды дня (scrubber/collage/kanban/pdf/markdown/tldr). В settings_hub + nav i18n.
- [x] A3. «Спросить про этот день»: `POST /api/day/{date}/ask` — LLM-ответ по контексту дня
  (hourly_card этого дня + сэмпл OCR + tldr), стрим или просто JSON. Блок-чат на странице дня.
- [x] A4. Граф→день: в memgraph detail-popup добавить кнопку «📅 К этому дню» для prompt/answer/
  session/summary/memory узлов → `/day/{date}` (день берём из timestamp узла). memory_graph.py +
  memgraph.js.
- [x] A5. `/memory`: часовые карточки и строки daily pins сделать кликабельными → `/day/{date}`
  (+ «View day» ссылка). memory.html.
- [x] A6. Унификация навигации к дню: cal_nav.js, /calendar, /stats heatmap, screenshot «jump to day»
  → ведут на `/day/{date}` (canonical). Старые /timeline?date= оставить рабочими.
- [x] A7. Дашборд/навбар: плитка/кнопка «📅 День» → `/day/сегодня` + поле выбора даты.

## ФАЗА B — Аналитика (удобная)
- [x] B1. `/analytics` (или апгрейд /stats): тренды активности (день/неделя), топ-приложения,
  использование ИИ (вызовы/токены/оценка стоимости из llm_usage по провайдерам/kind), покрытие
  захвата (часы с данными), звук-минуты, динамика памяти/чатов. Reuse `_window_start_iso`.
- [x] B2. JSON-эндпоинты для графиков (`/api/analytics/*.json`).
- [x] B3. Фильтры периода (7/30/90 дней) + переход на конкретный день из любой точки. settings_hub + nav i18n.

## ФАЗА C — Лендинг: блоки + страницы (по данным ресёрча конкурентов)
- [x] C1. Обновить блок сравнения на лендинге реальными данными (Persona vs Rewind/Limitless vs
  Microsoft Recall vs open-source) — из artifacts/competitors (ресёрч).
- [x] C2. `/features` — глубокая страница всех фич с разбивкой (захват, память, чат, граф, голос,
  брифинги, напоминания, приватность, своя модель).
- [x] C3. `/compare/rewind` (+ др. при наличии данных) — детальное сравнение по фичам.
- [x] C4. `/pricing` — честно: open-source/локально бесплатно; облачные модели = по ключу пользователя.
- [x] C5. `/security` + `/privacy-policy` + `/terms` (юр./безопасность, local-first акцент).
- [x] C6. `/roadmap` + `/changelog` (из git/версий).
- [x] C7. Блок отзывов/use-cases (честно — без выдуманных цитат: сценарии использования).
- [x] C8. Новые блоки на главной: расширенный how-it-works, FAQ+, CTA. Все ссылки в _public_nav.

## ФАЗА D — Блог: много статей (правдиво, SEO по семантике, длина по глубине темы)
Темы из ресёрча + продуктовые гайды. Markdown в `app/web/content/blog/*.md` (front-matter).
- [x] D1. «Persona vs Rewind/Limitless: local-first против облака».
- [x] D2. «Зачем локальная память экрана и почему приватность решает».
- [x] D3. «Как обучить свою модель (вторую копию) на Qwen3-4B-Thinking».
- [x] D4. «Как пользоваться Persona: захват → память → чат» (онбординг-гайд).
- [x] D5. «Граф памяти: как читать и навигировать свою жизнь».
- [x] D6. «Брифинги и напоминания: проактивный ассистент».
- [x] D7. «Bi-temporal память и mem0: почему факты не противоречат».
- [x] D8. «Microsoft Recall vs Persona: чем отличается».
- [ ] D9+. Доп. темы из синтеза ресёрча (по мере поступления).

## ФАЗА E — Ревью, тесты, дебаг, финал
- [ ] E1. Прогон всего pytest + golden-eval (память не просела).
- [ ] E2. Ревью: новые роуты под auth-gate/owner, jinja-компиляция всех новых шаблонов,
  миграции идемпотентны 2×, нет битых ссылок навигации день↔граф↔память.
- [ ] E3. Дебаг найденного.
- [ ] E4. Финальный отчёт: что сделано/не сделано, +коммиты, итог LOC.

---

## Прогресс исполнения
- B1-backend ✅ (v2.20.35): app/analytics_overview.py — get_analytics(days=7/30/90): посуточная серия
  (скрины/звук-мин/использований ИИ/токены), итоги (+покрытие захвата %), топ-приложения, llm_usage по
  kind/провайдеру. Фильтр/группировка через date(col,'localtime') (формат-агностично). 2 pytest. Дальше —
  страница /analytics (роут+шаблон) закроет B1.
- 2026-06-16: план создан. Инвентаризация (4 субагента) + ресёрч конкурентов (4+синтез) проведены.
  Вывод: фичи в основном есть кусками → акцент на унификацию + навигацию + страницу ДНЯ.
- Ресёрч-сводка для Фаз C/D сохранена в `.agent_designs/competitors_brief.md` (НЕ коммитить).
  КЛЮЧЕВОЙ КРЮЧОК: Rewind/Limitless куплен Meta (5.12.25), Mac-запись off с 19.12.25, EU/UK отрезаны →
  Persona local-first/open «нельзя купить и слить». «Hermes» = open-source локальный агент (не носимое).
  Готовы: таблица сравнения, 12 преимуществ, 20 заголовков, 9 возражений, 15 тем статей.
- A1 ✅ (v2.20.28): app/day_overview.py — агрегатор дня (скрины/OCR/звук/ИИ-использование/часы/часовые
  карточки/tldr/бюджет/топ-приложения), устойчив (try/except на блок), datetime()-нормализация времени
  для chat/tools/voice. Поймал и починил: audio_segment реально captured_at/duration_seconds (инвентаризация
  дала устаревшие started_at/duration_s). 3 pytest.
- A2 ✅ (v2.20.29): страница /day/{date} (day_overview_page.py + day_overview.html) — шапка с навигацией
  по дням, TL;DR, KPI-плитки (скрины/звук «записан да-нет»/ИИ использований+токены/часы), топ-приложения,
  блок «спросить про день» (форма готова, обработчик в A3), часовые карточки, галерея скринов по часам
  (reuse _screenshot_card + _group_by_hour), быстрые ссылки на scrubber/collage/kanban/pdf/таймлайн.
  Роут зарегистрирован, не затирает /day/{day}/collage|kanban|md|pdf.
- A3 ✅ (v2.20.30): POST /api/day/{date}/ask — LLM-ответ про день. Контекст _day_context = статистика дня
  + tldr + топ-приложения + часовые карточки + сэмпл OCR (до 6000 симв). Системный промпт «не выдумывай».
  Graceful без LLM (missing_config). answer_about_day(client=...) инжектируемый → 3 pytest (контекст/инъекция/
  пустой вопрос). Форма на /day/{date} теперь работает.
- A4 ✅ (v2.20.31): граф→день. В detail-popup узла кнопка «📅 К этому дню» → /day/{дата из n.at}
  (dayFromAt — локальная дата из timestamp; для day-узлов скрыта, т.к. их основной переход и так на день).
  memgraph.js + memory_graph.html (+ ?v= cache-busting на memgraph.js). JS-синтаксис node --check OK.
- A5 ✅ (v2.20.32): /memory — часовые карточки (hour_start[:10]) и строки daily pins (p.day) теперь ссылки
  на /day/{date} («день →» у карточек, кликабельная дата у pins). memory.html. jinja OK.
- A6 ✅ (v2.20.33): навигация к дню унифицирована на /day/{date}. cal_nav.js (клик по дню + «сегодня»:
  /timeline/{date} → /day/{date}, + ?v= cache-busting в base.html), calendar.html (клетки /?date= → /day/),
  stats.html year-heatmap (/?date= → /day/). node --check + jinja OK; старые /?date|/timeline/ ссылки убраны.
- A7 ✅ (v2.20.34): кнопка «📅 День» в навбаре (base.html more_items) + редирект GET /day → /day/{today}
  (day_overview_page.py, current_user_required) + i18n nav_day (ru День/en Day/de Tag). Routes /day и /day/{date}
  оба зарегистрированы без конфликта. ⭐ ФАЗА A (единая страница ДНЯ + сквозная навигация) ПОЛНОСТЬЮ ЗАКРЫТА.
- B1/B2/B3 ✅ (v2.20.36): страница /analytics (analytics_page.py + analytics.html) — переключатель 7/30/90,
  KPI (скрины/звук/ИИ/токены/покрытие%), посуточные div-бары (скрины + использование ИИ, клик → /day/{date}),
  топ-приложения (бары), ИИ по kind/провайдеру. В nav (ru/en/de nav_analytics) + settings_hub (Диагностика) + поиск.
  B2: server-render баров (отдельный JSON-эндпоинт не требуется). B3: фильтр периода через ?days + переходы на день.
  ⭐ ФАЗА B (аналитика) ЗАКРЫТА.
- C1 ✅ (v2.20.37): блок сравнения на landing.html обновлён реальными данными (Persona vs Rewind/Limitless vs
  Microsoft Recall vs open-source, 9 строк, колонка Persona .hi) + «крючок» «Декабрь 2025: Rewind отключён,
  Limitless у Meta, EU/UK отрезаны». landing.html рендерится. Фаза C старт.
- C2 ✅ (v2.20.38): публичная /features (landing.py route + features.html, standalone+_public_nav+landing/style.css) —
  12 преимуществ из брифа (local-first/нельзя купить, приватность, память «что реально делал», вторая копия, чат+
  bi-temporal, граф, проактивность, кроссплатформа, без NPU, без подписки, голос, цельный продукт) + крючок дек.2025 +
  CTA. Добавлены публичные префиксы Фазы C в auth_gate allow-list (/features,/compare,/pricing,/security,/privacy-policy,
  /terms,/roadmap,/changelog). «Возможности» в _public_nav → /features. Открывается без логина.
- C3 ✅ (v2.20.39): детальные сравнения /compare/{slug} (rewind, recall) — параметризованный роут в landing.py
  (_COMPARE данные из брифа) + compare.html (standalone+_public_nav). 9 критериев таблицей, крючок, вывод, CTA,
  перекрёстные ссылки. Публичные, рендер обоих ОК.
- C4 ✅ (v2.20.40): публичная /pricing (landing.py + pricing.html) — 3 честные карточки (Локально=бесплатно,
  Облако=по своему API-ключу/BYO, Self-host=открыто) + блок «сколько берут другие» (Rewind $19–50/мес+кулон,
  Recall=Copilot+ PC) + subscription fatigue. Без вранья про несуществующие платные планы. «Цена» в _public_nav. 200 анонимно.
- C5 ✅ (v2.20.41): публичные /security, /privacy-policy, /terms (3 роута в landing.py через add_api_route +
  параметризованный legal.html, данные _LEGAL). Local-first акцент, честно (без выдуманных сертификаций/гарантий,
  «это не юр.консультация»), дата обновления, перекрёстные ссылки. Все 3 публичные.
- C6 ✅ (v2.20.42): публичные /roadmap (готово/в работе/планируется) и /changelog (ключевое 2.20.x) —
  infopage.html (generic секции с буллетами) + данные _INFO в landing.py (add_api_route). Честно, без дат. Оба публичные.
- C7+C8 ✅ (v2.20.43): landing.html — секция use-cases (5 сценариев: клиент 3 недели назад / вернуть контекст /
  саммари встречи без бота / утренний бриф / найти мельком виденное) + FAQ расширен 7 возражениями из брифа
  (купят как Rewind / лог утечёт / локальная модель тупее / ресурсы / звук законность / данные при уходе / цена→pricing).
  Рендерится. ⭐ ФАЗА C (лендинг) ЗАКРЫТА: сравнение + /features + /compare/* + /pricing + /security,/privacy-policy,
  /terms + /roadmap,/changelog + use-cases + FAQ.
- D1 ✅ (v2.20.44): статья блога persona-vs-rewind-limitless.md (категория Сравнения, ~4 мин) — факты Rewind→Meta
  (5.12.25, Mac off 19.12.25, EU/UK), таблица, чем Persona отличается архитектурно, что выбрать, как перейти.
  Правдиво по брифу. Парсится в list_posts. Фаза D старт (1/15).
- D2+D3 ✅ (v2.20.45): статьи zachem-local-first-pamyat.md (Приватность — манифест на кейсе Rewind→Meta, data
  sovereignty, subscription fatigue, иммунитет open-source) и kak-obuchit-vtoruyu-kopiyu.md (Гайды — QLoRA пошагово:
  трезвые ожидания, датасет, база под железо, обучение bf16/lora, Ollama; реальный пайплайн Qwen3-Thinking). Обе в list_posts. Фаза D: 3/15.
- D4+D5 ✅ (v2.20.46): kak-nachat-za-5-minut.md (Гайды — онбординг: агент Mac/Win, захват+исключения, наполнение
  памяти, первый вопрос, страница дня) и chat-s-pamyatyu-bitemporal.md (Технологии — кросс-чат recall + bi-temporal
  факты простыми словами, таблица vs поиск, приватность локальна). Обе в list_posts. Фаза D: 5/15.
- D6+D7 ✅ (v2.20.47): nastroika-privatnosti-zahvata.md (Приватность — защита устройства, исключения, redaction,
  пауза/удаление/retention, air-gapped Ollama, граница облака; честно «не магия») и skolko-vesit-i-tormozit.md
  (Технологии — дедуп-скриншоты не видео, рычаги объёма/нагрузки таблицей, тиры памяти; порядки величин без выдумок). Блог 7/15.
- D8+D9 ✅ (v2.20.48): brifing-i-nl-napominaniya.md (Гайды — бриф из часовых карточек, фидбек 👍/👎, тихие часы,
  NL-напоминания, таблица vs календарь) и open-source-protiv-pogloshcheniya.md (Приватность — обещание vs
  архитектура, кейсы Rewind→Meta/Recall, open-source+local-first = иммунитет). Блог 9/15.
- D10+D11 ✅ (v2.20.49): persona-vs-screenpipe-openrecall.md (Сравнения — честно: open-source даёт захват, Persona
  добавляет чат+граф+голос+брифинги+свою модель; таблица; кому что) и graf-pamyati.md (Технологии — узлы/связи,
  кросс-доменные инсайты, «К этому дню», локальность, граница честности). Блог 11/15.
