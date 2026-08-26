# Persona — заметки для Claude

## Мульти-юзер (ВАЖНО, с v2.31)
Регистрация открыта всем. Не-владелец = **member**: чат, память, граф,
голос, скиллы, свой промпт/тема/язык, профиль. Захват, таймлайн, поиск,
заметки, напоминания, дашборды, Telegram, мышление, сны — **только владелец**
(данные захвата пока глобальные, одно-тенантные).

- **Настройки юзера — в таблицу `user_settings`** (миграция 228) через
  `get_user_kv`/`set_user_kv` (+ `get_user_kv_sync` в шаблонах), а НЕ в
  глобальный `kv`. Запись в глобальный `kv` из пути, куда доходит member, —
  это утечка настроек владельца.
- **Новый роут, доступный member'у**, обязательно добавить в `_MEMBER_PREFIXES`
  (`app/web/middleware/auth_gate.py`). Вне allowlist: `/api/*` → 403, страницы
  → редирект на `/chat`. Такой роут не должен читать глобальный `kv` и не
  должен ходить в БД без фильтра по `user_id` (иначе покажет чужое).
- **`make_client()` обязан получать `user_id`** на любом пути, куда может
  дойти member: не-владелец резолвит провайдера/ключ только из своего
  `user_settings` (без fallback на `kv`/env/vault), провайдер `worker` (ПК
  владельца) запрещён (`LLMProviderForbidden`), Ollama — только свой URL.
- **Новая страница настроек, видимая member'у**, добавляется в
  `_MEMBER_CATEGORIES` в `app/web/routes/settings_hub.py` (в дополнение к
  `_CATEGORIES` для владельца).

## Настройки (ВАЖНО)
Любую НОВУЮ страницу, связанную с настройками (`/settings/*` или иной
конфиг-экран), ОБЯЗАТЕЛЬНО добавлять в хаб настроек:
`app/web/routes/settings_hub.py` → список `_CATEGORIES` (нужная категория,
кортеж `("/путь", "описание")`). Иначе пункт недоступен из UI. По желанию —
ещё и в навбар `app/web/templates/base.html` (`more_items`) + перевод
`nav_*` в `app/translations/{ru,en,de}.json`.

## Секреты (ВАЖНО — репозиторий готовится к публикации)
Один раз на клон: `sh ops/install_hooks.sh` (или
`powershell -File ops\install_hooks.ps1`) — ставит pre-commit хук
`ops/hooks/pre-commit`, который блокирует коммит с ключом/токеном/приватным
ключом/файлом `.env`. Тот же набор правил гоняет `pytest` через
`tests/test_no_secrets_committed.py`, так что незаустановленный хук не спасает
от красных тестов. Правила и escape-hatch (`# secret-scan: ignore`,
`PERSONA_SKIP_SECRET_SCAN=1`) — в `docs/SECRET_HYGIENE.md`.

Секреты живут ТОЛЬКО в `.env` (в `.gitignore`) или в `PERSONA_DATA_DIR`
(`~/.persona/`, вне репо). Никаких реальных дефолтов в коде, никакого
логирования значений токенов. `.env.example` — шаблон с ПУСТЫМИ значениями.

## Почта (ВАЖНО — на этом сервере SMTP невозможен)
Исходящие 25/465/587/2525 закрыты файрволом **по порту** (замер 2026-08-26:
один и тот же IP отвечает на 443/2053/2087/8443 и отвергает 25/465/587/2525).
Поэтому «поменять SMTP-провайдера» не поможет никогда — нужен HTTPS.

- Транспорт выбирается настройкой `mail_transport` (kv) / `PERSONA_MAIL_TRANSPORT`
  (env): `smtp_starttls` (дефолт, прежнее поведение) | `smtp_ssl` | `http_api`.
  Реализация — `app/mail_transport.py`, там же замеры и обоснование Resend.
- Ключ провайдера — ТОЛЬКО `PERSONA_RESEND_API_KEY` или
  `{PERSONA_DATA_DIR}/resend_api_key`. **Никогда в БД и никогда в лог**
  (`mail_transport.scrub`).
- `delivery_status()` отвечает `ok` только если конфиг полон И до транспорта
  доходит TCP; иначе `unreachable` / `no_credentials`. Не возвращай сюда
  «ok = поля заполнены»: именно это обещало письма, которых не будет.
- Любой новый вызов почты обязан оставаться best-effort: статус-словарь,
  никаких исключений в запрос, потолок `PERSONA_MAIL_TIMEOUT` (деф. 10 с).

## Релиз
Каждый фич-коммит: бамп `app/__init__.py __version__` И `CACHE_VERSION` в
`app/web/static/sw.js` (одинаковая версия). Статик-ассеты лучше подключать с
`?v={{ app_version }}` (cache-busting) — иначе браузер/Service Worker отдаёт
старое.

## Темы
Темы: `dark / light / auto / persona / cosmos / cosmos-dark`. Регистрируются в
`app/web/templates_engine.py` (`_THEME_VALUES`), `app/web/routes/theme.py`
(`_VALID_THEMES` + options) и `app/web/templates/base.html` (класс `<html>` +
условная подгрузка css/js темы).

## Чат: режимы и промпт
- «Расширенный режим» (kv `advanced_mode`) + по-фичам `feat_*` —
  `get_advanced_flags()` в `app/web/routes/chat_sessions.py`. Выкл → простой
  ассистент-друг (без кода/планов/инструментов).
- Характер задаётся в `/settings/system-prompt` (kv `chat_system_prompt`,
  пресеты в `app/chat/prompts.py`). Простой режим использует сохранённый промпт
  если он кастомный, иначе `FRIEND_PROMPT`.
- Перед ответом подтягивается память по всем чатам. Режим — kv `recall_mode`
  (`get_recall_mode()` в `chat_sessions.py`): **по умолчанию `keyword`** →
  `recall_relevant()`/`recall_by_terms()` в `app/chat/sessions.py` (FTS5 bm25 +
  LIKE-fallback, поиск по именам/ключевым словам). Режимы `hybrid`/`vector`
  (FTS5 bm25 + векторный KNN через RRF, `app/memory_vec.py`) — **опциональны и
  по умолчанию ВЫКЛ**: требуют `pip install sqlite-vec` + Ollama-embed-модель
  (`embed_model`, деф. `nomic-embed-text`) + бэкфилл индекса (`index_message`);
  без них тихий fallback на keyword. Личные факты о пользователе — `user_memory`
  (`app/chat/user_memory.py`), подмешиваются отдельным блоком.
- С v2.31.7 `advanced_mode`/`feat_*` и `chat_system_prompt` для не-владельца
  берутся из `user_settings` (дефолты), а не из глобального `kv`; глобальный
  `kv` остаётся только у владельца. Векторный индекс (`hybrid`/`vector`) —
  **только владелец**: эмбеддинги резолвят глобальный конфиг, поэтому текст
  member'а не должен попадать на железо владельца.
