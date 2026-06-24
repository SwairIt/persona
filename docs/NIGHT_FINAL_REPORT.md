# Persona — отчёт ночного автономного билда (2026-06-24)

> Оркестратор: Claude (Opus 4.8) в ralph-loop. Работал всю ночь без остановок,
> субагентами, локальные коммиты (НЕ пушил по твоей просьбе). Версия: 2.20.65 → **2.20.75**.
> 12 коммитов (`6a806dc..HEAD`). Полный план — `docs/NIGHT_BUILD_PLAN.md`,
> исследование памяти — `docs/MEMORY_RESEARCH.md`, i18n-аудит — `docs/I18N_AUDIT.md`.

## TL;DR — что изменилось
1. **Безопасность** — закрыты ВСЕ 27 подтверждённых дыр аудита перед публичным доменом.
2. **Память как у Hermes** — ночной воркер-«сон» (рефлексия), salience-scoring recall, importance,
   фикс эмбеддингов, UI управления памятью. Это главный запрос — сделан полностью.
3. **Русский по умолчанию** + локализованы ~33 страницы (982 ключа, паритет en/ru/de).
4. Сервер в проде на v2.20.75, всё верифицировано (compileall, import, миграции, паритет, smoke).

---

## По фазам (с коммитами)

### Ф1 — Безопасность (коммиты 6a806dc, 66d90c4)
Полный аудит (146 агентов в 2 прохода) → 27 подтверждённых дыр, все закрыты:
- **КРИТ:** `/api/export/full.zip` (дамп всей БД без авторизации), `/thumbs/*` (вся история
  скриншотов анониму через allow-list), plaintext API-ключи в HTML — закрыты.
- Defense-in-depth `current_user_required` на ~25 приватных роутов (401 независимо от gate).
- cookie `Secure` через X-Forwarded-Proto, rate-limit на `/auth/*`, magic-link больше не
  создаёт аккаунты вслепую, SSRF-гард (`app/net_guard.py`) на webhooks + CSV-pipeline,
  install-токен single-use, `/health` без host/port/db_error.
- Проверено вживую: full.zip/settings/multishot→401, thumbs/diag/doctor→303.

### Ф2 — Research памяти → docs/MEMORY_RESEARCH.md
7 веб-агентов: Letta/MemGPT, mem0/Zep/Graphiti, Generative-Agents/Reflexion, Hermes nightly,
Yandex, local-LLM-паттерны, salience/forgetting → синтез на реальном коде Persona.

### Ф3 — Память (ПОЛНОСТЬЮ: b364a48, 8cbb969, a918b3e, 4a7eadb, 8227e8e)
- **A:** фикс эмбеддингов nomic (task-префиксы `search_query/document:` + `num_ctx=8192`) —
  чинит тихую деградацию retrieval.
- **D (ядро запроса):** ночной воркер-«сон» (`app/workers/dream_worker.py`,
  `app/chat/reflection.py`, `app/dreams.py`, migration 191). Раз в ночь LLM просматривает
  день (чат + экран/OCR + речь), извлекает durable-факты, консолидирует, пишет инсайты.
  **OPT-IN** (по умолчанию выкл) — включается на `/settings/memory`.
- **B:** salience-scoring recall (migration 192, `score_and_rerank` recency·importance·relevance
  + MMR против перефразов, режим `generative`). Keyword остаётся дефолтом.
- **C:** importance при записи (эвристика salience) — важные факты всплывают в промпт.
- **F:** `/settings/memory` — тумблер «сна», час, режим recall, веса, просмотр/забыть рефлексии.

### Ф4 — Русский по умолчанию (01e29f6)
`DEFAULT_LANGUAGE=ru` + robust-фолбэк ru→en→key. Локализованы базовые поверхности.

### Ф5 — UI/UX + локализация (c1323b5, 78a4a15, 1e07c5e)
- Settings hub (`/settings/hub`) проверен — уже отличный (клиентский поиск+категории), навбар ведёт туда.
- Локализованы 33 страницы настроек/админки/обзоров (885→982 ключа), `<html lang>`→ru на 9 файлах.
- low-sev: `/health` payload-трим.

---

## ✅ Верификация (на момент отчёта)
- `python -m compileall app` — OK (вся кодовая база).
- `import app.web.main` — OK.
- Миграции на реальной БД (init_database повторно) — идемпотентны, OK.
- i18n паритет: `set(en)==set(ru)==set(de)` = 982 ключа.
- **Тесты:** 10 новых юнит-тестов на ночной код (память: salience/scoring/dreams;
  безопасность: SSRF-гард/rate-limit) — зелёные (`tests/test_night_build.py`).
  Полный pytest-набор — **546 passed, 15 skipped, 0 failed** (после обновления
  11 тест-файлов под owner-only роуты Ф1, коммит 55bfec4 — security-фиксы
  потребовали авторизации в тестах).
- Security smoke (v2.20.75): full.zip/settings/multishot→401, thumbs/doctor→303,
  `/health` без host/port. Сервер в проде (autostart-респаун).

---

## ⚠️ ЧТО НУЖНО СДЕЛАТЬ ТЕБЕ

### Перед публичным доменом (КОД закрыт, но эти пункты — только ты):
1. **РОТИРОВАТЬ утёкшие креды** (они в git-истории, единственный фикс — перевыпуск):
   3 GitHub PAT (в `.git-credentials`) + ключ ElevenLabs (был в `video-use/.env`).
   После ротации: `git config --global credential.helper manager-core` + удалить `.git-credentials`.
2. **Зарегистрировать owner-аккаунт ДО** того как сайт станет публичным (иначе кто первый
   зарегался — тот владелец).
3. На реверс-прокси FastPanel: TLS + HSTS + security-заголовки + rate-limit.

### Чтобы включить ночную память («сон» как у Hermes):
- Открой `/settings/memory` → включи тумблер «Ночная рефлексия», выбери час.
- Нужна поднятая Ollama-модель (devtunnel `persona-llm`). Без неё — тихий fallback.
- При включённом vector/hybrid recall сделай реиндекс (`backfill_index`) под новые nomic-префиксы.

### GitHub-пуш
Чинён (username SwairIt вшит в remote URL), но по твоей просьбе НЕ пушил. 12 коммитов локально
на master. Когда захочешь — `git push origin master` (после ротации PAT в п.1).

---

## 🗺 Ф6 — большие фичи (НЕ строил без твоего ревью — обоснование)
Эти фичи из плана `happy-hugging-deer.md` — крупные и рискованные для автономной ночной
сборки (трогают ядро чата/auth, нужны твои дизайн-предпочтения «разные дизайны»). Ядро
«как у Hermes» (память) уже сделано. Рекомендуемый порядок, когда будешь ревьюить вживую:
1. **Окно активности ИИ** (`/ai-activity` уже есть каркас) — показывать что делает ИИ live.
2. **Агентный цикл/планы** — вынести из `chat_sessions.py`, план-фаза + самопроверка.
3. **Голос по умолчанию** — браузерный STT/TTS (страница `/voice` есть).
4. **Root-панель** `/root` + роли + live-логи — меняет auth_gate, нужны тщательные тесты.
5. Браузер-агент + MCP-рантайм — большой, нужен Node.

## Бэклог (мелочи, безопасно доделать)
- i18n-остаток: ~150 редких diag/dev-страниц (низкий трафик) — малыми батчами ≤8.
- low-sev Ф1 хвост: HSTS (лучше на FastPanel-прокси), GET `/auth/logout`→POST.
- Ф3-E (опц.): RAG по документам + Reflexion на 👎.
- 3 шаблона (api_tokens/settings_backup/vault) — локализованы (78a4a15).

## Операционные заметки
- **Рестарт:** `taskkill /T` дерева uvicorn → ПОДОЖДАТЬ 60-90с → autostart САМ поднимает новый
  код с диска. Ручной старт не нужен (проигрывает бинд-гонку).
- На каждый фич-коммит: бамп `app/__init__.py __version__` + `CACHE_VERSION` в `sw.js`.
