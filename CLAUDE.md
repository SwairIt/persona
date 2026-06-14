# Persona — заметки для Claude

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
- Перед ответом подтягивается память по всем чатам: `recall_relevant()` в
  `app/chat/sessions.py` (поиск по именам/ключевым словам).
