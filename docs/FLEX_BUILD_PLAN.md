# Persona — Flex Build (мандат 2026-06-30)

Источник: understanding-воркфлоу `wzc8tro2s` (6 карт + синтез). Задачи юзера:
(1) гибкость/запуск где угодно, (2) скрины+аудио+обработка, (3) граф как Hermes,
(4) монитор нагрузки ПК, (5) «обучить память» — долгий ночной прогон.

## Слайсы (непересекающиеся файлы → параллельно)

| ID | P | Заголовок | Владеет файлами | Группа |
|----|---|-----------|-----------------|--------|
| S1 | P0 | psutil-сборщик метрик ПК | `app/system_metrics.py` (new) | A |
| S2 | P0 | Страница `/settings/system-monitor` | `app/web/routes/system_monitor.py`+`templates/system_monitor.html` (new) | A |
| S3 | P1 | Метрики ПК в health-dashboard+Prometheus | `app/health_dashboard.py`, `templates/_health_fragment.html`, `app/metrics_extended.py` | A |
| S4 | P0 | Кнопка «Обучить память сейчас» | `app/web/routes/memory_settings.py` | B |
| S5 | P0 | Многопроходная консолидация+кластеризация+отчёт | `app/chat/reflection.py`, `app/chat/user_memory.py`, mig `196` | B |
| S6 | P0 | Семантический граф (рёбра+триплеты) | `app/web/routes/memory_graph.py`, `app/chat/reflection.py`, mig `197`, `app/knowledge_graph.py` (new) | B (после S5) |
| S7 | P1 | Salience во всех recall-режимах | `app/memory_vec.py`, `app/web/routes/memory_settings.py` | C (после S4) |
| S8 | P1 | `/api/health/full` | `app/web/routes/health.py` | D |
| S9 | P1 | Портативный watchdog | `ops/persona_watchdog.py`, `ops/install_watchdog_windows.py` (new) | E |
| S10 | P2 | Dockerfile+compose | `Dockerfile`,`docker-compose.yml`,`.dockerignore`,`README.md` | F |
| S11 | P2 | Параллельный OCR (ProcessPool) | `app/workers/ocr_worker.py` | G |
| S12 | P2 | FTS5 аудио + ретранскрипт-бэкфилл | `app/web/routes/audio_search.py`, mig `198`, `app/workers/transcribe_backfill_worker.py` (new) | G |

## Hub-файлы (правит ТОЛЬКО оркестратор после агентов)
`app/__init__.py`, `app/web/static/sw.js`, `app/translations/{ru,en,de}.json`,
`app/web/main.py`, `app/web/routes/settings_hub.py`. Агенты возвращают манифест
(translationKeys / mainPyRegistration / settingsHubEntry / migration).

## Порядок
Batch 1 (||): S1,S2,S3,S4,S5,S8,S9,S10,S11,S12. Batch 2 (||): S6,S7.
Затем: консолидация hub-файлов → бамп версии → гейт (import+py_compile+тесты+
миграции дважды) → коммит RU → push → деплой watchdog.
