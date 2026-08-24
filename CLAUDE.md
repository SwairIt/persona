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
