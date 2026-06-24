# Persona — Ночной автономный билд (ralph-loop master plan)

> Запущено 2026-06-24. Оркестратор — Claude (Opus 4.8). Пользователь спит, вопросов не задаём,
> решения принимаем сами, выбираем разумные дефолты. Работаем не останавливаясь до `PERSONA_NIGHT_DONE`.

## Роль и режим
Я главный. Делаю проект Persona (личная AI-память: FastAPI + aiosqlite WAL + Jinja2/Alpine/Tailwind,
корень `c:\www-Yaroslav\Persona`) идеальным: безопасность, единая память с ночной рефлексией, русский
по умолчанию, удобный UI, ключевые фичи. Независимые куски — параллельными субагентами (Agent tool).
Сложный research / перепроверку — несколько субагентов с состязательной верификацией.

## Протокол каждой итерации (ralph)
1. Прочитать этот файл (раздел «Прогресс» внизу) + todo-список.
2. Выбрать следующую незакрытую задачу по приоритету фаз (Ф1→Ф7).
3. Реализовать (субагенты для независимого; сам — для деликатного/связного).
4. **Верифицировать ДО «готово»**: `.venv\Scripts\python.exe -c "import app.web.main"` + `py_compile`
   затронутых файлов. При изменении рантайма — перезапустить uvicorn (см. ниже).
5. Фич-коммит → бамп `app/__init__.py __version__` И `CACHE_VERSION` в `app/web/static/sw.js` (одинаково).
6. Коммит локально, по-русски, с `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. **НЕ пушить.**
7. Обновить «Прогресс» внизу этого файла + todo.

## Команды
- Импорт-чек: `c:\www-Yaroslav\Persona\.venv\Scripts\python.exe -c "import app.web.main"`
- Рестарт (память `dev-server-restart`): убить WORKER-детей раньше родителей (иначе отдают старый код на :8000),
  потом `Start-Process .venv\Scripts\python.exe -m uvicorn app.web.main:create_app --factory --host 0.0.0.0 --port 8000` detached.
- Перед публичным доменом запуск добавит `--proxy-headers --forwarded-allow-ips=*`.

## Гардрейлы (жёстко)
- НЕ пушить в GitHub. НЕ делать `reset --hard` / `push --force` / удаление данных/БД.
- НЕ трогать секреты пользователя, не ротировать токены за него.
- Следовать `CLAUDE.md`: новые `/settings/*` → `settings_hub.py _CATEGORIES`; темы регистрировать в 3 местах;
  новые строки UI → ключи в `app/translations/{ru,en,de}.json`.
- Тон чат-ассистента — живой друг (память `assistant-friend-tone`), не корпоративный AI.
- Дизайн — разный под каждую поверхность (память `design-varied-not-one-template`), не один шаблон.
- Коммиты гранулярные; держать сервер запускаемым на каждом срезе.

---

## Ф1 — БЕЗОПАСНОСТЬ (дозакрыть 27 подтверждённых дыр аудита wf_77ef6f99)
Корневая причина большинства: роуты полагаются только на fail-open `auth_gate` (выключен в bootstrap-окне
до первого signup). Фикс: defense-in-depth `current_user_required` на приватные роуты (401 независимо от gate).

**Уже сделано субагентом (проверить, не откатилось ли):** router-level `dependencies=[Depends(current_user_required)]`
на: settings_api, capture_api, multi_shot_zip, diag_bundle (+ расширены needles в `app/diagnostics_bundle.py`),
doctor, demo_seeder, external_ping, webhooks_routes, llm_switcher, audio_segment, audio_player. (12 файлов, компилируются.)

**Осталось (чеклист):**
- [ ] auth на mixed-роуты per-handler (НЕ ломая публичные): `public_day.py` (3 admin-хендлера, не трогать `/public/day/{slug}`),
      `mic_toggle.py` (только POST, GET оставить публичным для агента), `shot_share.py` (create/revoke, не view),
      `share_collection.py` (create), `shot_compare.py` (compare page+json), `side_by_side.py` (`/compare/{a}/{b}`).
- [ ] `auth_gate.py` _PUBLIC_PREFIXES → точный allow-list (exact-set ИЛИ subtree с `/` на конце), чтобы `/compare`
      и `/thumbs/` НЕ светили скриншоты анонимам даже после signup. Убрать bare `/compare`; `/thumbs/` — см. ниже.
- [ ] КРИТ: `/thumbs/{path}` (`thumbnails.py`) — добавить `current_user_required`; убрать `/thumbs/` из allow-list;
      для публичных шар/public-day сделать отдельный токен-скоупленный роут эскизов (или отдать через share-токен).
- [ ] `POST /api/audio/mic` — требовать auth на POST (GET остаётся публичным для поллинга агента).
- [ ] Cookie `Secure` — выводить из `X-Forwarded-Proto` (за TLS-прокси) + флаг `cookie_secure`; добавить HSTS;
      запускать uvicorn с `--proxy-headers --forwarded-allow-ips=*`.
- [ ] SSRF-гард (резолв host → отказ при is_private/loopback/link_local/reserved/multicast; redirects off):
      `app/webhooks/dispatcher.py` перед `client.post`; `app/webhook_csv_pipeline.py` в upsert_destination и перед urlopen.
- [ ] Rate-limit (in-process sliding window) на `/auth/magic`, `/auth/forgot`, `/auth/signup`, `/auth/login` (429);
      перестать авто-создавать аккаунт в `/auth/magic` для неизвестных email (в waitlist); GET `/auth/logout` → POST-only.
- [ ] `install.py` — сделать install-токен реально single-use (удалять kv-строку `install_pending_<id>` после выдачи
      скрипта; добавить `delete_kv` в repository); TTL 600→180; `Referrer-Policy: no-referrer` на install-странице.
- [ ] `/health` payload — убрать host/port/db_error/captures_total из анонимного ответа (оставить status/version/db_ok);
      `str(exc)` только в лог.
- [ ] Bootstrap (доп. защита): gate fail-CLOSED для не-публичных путей при пустой users-таблице (пускать только
      public + `/auth/` + `/setup`), и `/setup` POST привязать к owner/loopback/bootstrap-token.
- [ ] Верифицировать: `import app.web.main` чисто; smoke: `/api/export/full.zip`→401, `/thumbs/...`→401/403,
      `/api/settings.json`→401. Бамп версии, рестарт, коммит.

## Ф2 — RESEARCH ПАМЯТИ (субагенты + веб) → `docs/MEMORY_RESEARCH.md`
Топики: Яндекс-митап про память LLM (~2026-06-23); «Hermes» c ночным просмотром промптов/ответов и обучением
(что это за продукт, механизм); Letta/MemGPT (core/archival/recall memory, sleep-time agents, memory blocks);
mem0/Zep/Graphiti/Cognee (извлечение, граф, темпоральность, дедуп); Reflexion + Generative Agents (memory stream,
importance/recency/relevance retrieval, reflection); sleep-time compute / консолидация; память для локальных LLM
(Ollama 4–8B без файнтюна: что реально работает); salience/забывание/decay/конфликты/приватность. Каждый агент —
web-поиск + структурированные находки; финальный синтез-агент пишет `docs/MEMORY_RESEARCH.md` с конкретной
рекомендацией под Persona.

## Ф3 — ПАМЯТЬ (единая + ночная рефлексия «как у Hermes»)
- Слои: working (контекст сессии) / episodic (сообщения, скрин/OCR/аудио) / semantic (факты, `user_memory`) /
  procedural (предпочтения, как себя вести). Единый recall поверх FTS5 bm25 + векторный KNN (RRF, `memory_vec.py`).
- **Ночной воркер-рефлексия** (рядом с auto_digest): раз в ночь LLM на локальной Ollama просматривает диалоги +
  экран/OCR + аудио за день → извлекает durable-факты/предпочтения/сущности, консолидирует, чинит конфликты
  (новое перекрывает старое), апдейтит `user_memory` + индекс, ставит salience/recency. Без файнтюна модели.
- Починить кросс-чат recall окончательно (тест: сказать факт в чате A → спросить перефразом в чате B → находит).
- Управление: режим recall, вкл/выкл рефлексии, что попадает в память, ручной просмотр/правка/забывание.

## Ф4 — РУССКИЙ I18N
Русский по умолчанию. 100% покрытие строк в `app/translations/{ru,en,de}.json`; вычистить хардкод-английский
из шаблонов (найти субагентом по `templates/**`), завести ключи, подставить `{{ t('...') }}`. Переключатель языка.

## Ф5 — UI/UX
Единый settings-shell с поиском (план happy-hugging-deer Ф5: 8 секций), нормальные настройки, полировка всех
экранов, разные современные дизайны под поверхности (скилл `ui-ux-pro-max`), консистентность, мелочи, мобилка.

## Ф6 — КЛЮЧЕВЫЕ ФИЧИ (из `C:\Users\Yaroslav\.claude\plans\happy-hugging-deer.md`, по приоритету)
Агентный цикл/планы + самопроверка; расширенный каталог инструментов; голос по умолчанию (браузерный STT/TTS);
окно активности ИИ (что делает); root-панель + роли + live-логи. Брать срезами, каждый отдельно коммитить.

## Ф7 — ФИНАЛ
`import app.web.main` + миграции дважды на scratch-БД; полнота переводов; дыры закрыты (smoke); бамп версии;
рестарт; итоговый отчёт в конце этого файла. Затем вывести `PERSONA_NIGHT_DONE`.

---

## Прогресс (обновлять каждую итерацию)
- 2026-06-24 старт. Готово ранее: коммит 6a806dc (критические дыры), 12 файлов Ф1 router-level auth (uncommitted).
- [ ] Ф1 безопасность
- [ ] Ф2 research памяти
- [ ] Ф3 память + ночная рефлексия
- [ ] Ф4 русский i18n
- [ ] Ф5 UI/UX
- [ ] Ф6 ключевые фичи
- [ ] Ф7 финал → PERSONA_NIGHT_DONE
