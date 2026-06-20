"""Слэш-команды чата — единый реестр (источник правды для палитры и /help).

Зеркалит подход Claude Code: пользователь печатает `/команда [аргумент]` в
композере, всплывает палитра-автокомплит, выбор выполняет действие. Два типа:

* ``client`` — чисто UI-действие (новый чат, очистить, поиск, открыть память…),
  ход модели не нужен. Диспетч в ``chat_index.html`` (runSlashCommand).
* ``turn`` — директива на ОДИН ход: меняет режим/эффорт и/или префиксит текст,
  затем сообщение уходит модели. ``expand_command`` разворачивает её серверно.

Сервер отдаёт реестр через ``GET /api/chat/commands`` (палитра грузит оттуда —
имена не расходятся между клиентом и сервером). Команды для кода (/edit /run
/git) сознательно НЕ включены — заморожены (`[HOLD]`).
"""

from __future__ import annotations

from typing import Any

# Каждая запись: name (без слэша), aliases, type (client|turn), group, args
# (подсказка по аргументу, "" если нет), desc (для палитры и /help).
COMMAND_SPECS: list[dict[str, Any]] = [
    # ── Сессия / навигация (client) ──────────────────────────────────────
    {"name": "new", "aliases": ["n"], "type": "client", "group": "Сессия",
     "args": "[заголовок]", "desc": "Новый чат"},
    {"name": "title", "aliases": ["rename"], "type": "client", "group": "Сессия",
     "args": "<текст>", "desc": "Переименовать текущий чат"},
    {"name": "clear", "aliases": ["cls"], "type": "client", "group": "Сессия",
     "args": "", "desc": "Очистить поле ввода"},
    {"name": "search", "aliases": ["find"], "type": "client", "group": "Сессия",
     "args": "<запрос>", "desc": "Поиск по всем чатам"},
    {"name": "stop", "aliases": [], "type": "client", "group": "Сессия",
     "args": "", "desc": "Остановить генерацию"},
    {"name": "retry", "aliases": ["r"], "type": "client", "group": "Сессия",
     "args": "", "desc": "Повторить последнее сообщение"},
    # ── Режим / мощность (turn) ──────────────────────────────────────────
    {"name": "plan", "aliases": [], "type": "turn", "group": "Режим",
     "args": "[текст]", "desc": "Режим «План» на этот ход"},
    {"name": "ask", "aliases": [], "type": "turn", "group": "Режим",
     "args": "[текст]", "desc": "Режим «Спрашивать» на этот ход"},
    {"name": "auto", "aliases": [], "type": "turn", "group": "Режим",
     "args": "[текст]", "desc": "Режим «Авто» на этот ход"},
    {"name": "bypass", "aliases": [], "type": "turn", "group": "Режим",
     "args": "[текст]", "desc": "Режим «Без спроса» на этот ход"},
    {"name": "fast", "aliases": [], "type": "turn", "group": "Мощность",
     "args": "[текст]", "desc": "Эффорт «Быстро» на этот ход"},
    {"name": "normal", "aliases": [], "type": "turn", "group": "Мощность",
     "args": "[текст]", "desc": "Эффорт «Норма» на этот ход"},
    {"name": "deep", "aliases": [], "type": "turn", "group": "Мощность",
     "args": "[текст]", "desc": "Эффорт «Глубоко» на этот ход"},
    {"name": "web", "aliases": ["search-web"], "type": "turn", "group": "Мощность",
     "args": "<запрос>", "desc": "Найти в интернете и ответить"},
    # ── Память / контекст ────────────────────────────────────────────────
    {"name": "remember", "aliases": ["mem+"], "type": "client", "group": "Память",
     "args": "<факт>", "desc": "Запомнить факт о тебе"},
    {"name": "forget", "aliases": ["mem-"], "type": "client", "group": "Память",
     "args": "<факт|id>", "desc": "Забыть факт"},
    {"name": "memory", "aliases": ["mem"], "type": "client", "group": "Память",
     "args": "", "desc": "Что ИИ обо мне помнит (редактор)"},
    {"name": "persona", "aliases": ["role"], "type": "client", "group": "Память",
     "args": "", "desc": "Задать роль (system prompt)"},
    {"name": "skill", "aliases": ["skills"], "type": "client", "group": "Память",
     "args": "", "desc": "Навыки (установить/вкл/выкл)"},
    # ── Окна / навигация (client) ────────────────────────────────────────
    {"name": "activity", "aliases": ["acts"], "type": "client", "group": "Окна",
     "args": "", "desc": "Окно «что делает ИИ»"},
    {"name": "voice", "aliases": [], "type": "client", "group": "Окна",
     "args": "", "desc": "Настройки голоса"},
    {"name": "theme", "aliases": [], "type": "client", "group": "Окна",
     "args": "", "desc": "Темы оформления"},
    # ── Эксперт-навыки (turn + overlay) — в духе плагинов Claude Code ─────
    # Накладывают экспертную инструкцию на ОДИН ход (в системный промпт),
    # делая ассистента «умнее» под конкретную задачу. Текст overlay живёт
    # на сервере (command_overlay), клиент шлёт только имя команды.
    {"name": "review", "aliases": ["cr"], "type": "turn", "group": "Эксперт",
     "args": "<код|текст>", "desc": "Ревью: баги → упрощения",
     "overlay": (
         "Проведи тщательное ревью приведённого кода/текста. Сначала "
         "КОРРЕКТНОСТЬ и баги — с конкретным местом и почему это баг; затем "
         "упрощения, дубли, производительность. Только реальные находки, без "
         "воды и придирок ради придирок. Если кода нет в сообщении — попроси вставить."
     )},
    {"name": "explain", "aliases": ["ex"], "type": "turn", "group": "Эксперт",
     "args": "<что объяснить>", "desc": "Объяснить понятно и по делу",
     "overlay": (
         "Объясни понятно и по делу: что это, как работает и зачем нужно. От "
         "простого к деталям, с коротким примером если уместно. Без воды и без жаргона без нужды."
     )},
    {"name": "debug", "aliases": ["fix"], "type": "turn", "group": "Эксперт",
     "args": "<проблема>", "desc": "Системная отладка (причина → фикс)",
     "overlay": (
         "Действуй как при системной отладке, не угадывай фикс вслепую: "
         "1) сформулируй симптом и ожидаемое поведение; 2) выдвини гипотезы о "
         "корневой причине; 3) скажи, что проверить, чтобы подтвердить/опровергнуть; "
         "4) только потом предлагай исправление. Сначала причина — потом фикс."
     )},
    {"name": "brainstorm", "aliases": ["idea"], "type": "turn", "group": "Эксперт",
     "args": "<задача>", "desc": "Разобрать задачу до решения",
     "overlay": (
         "Не бросайся решать сразу. Сначала разбери задачу: уточни цель, "
         "ограничения и неявные требования (задай 2–4 точных вопроса, если "
         "чего-то не хватает). Затем предложи несколько вариантов с плюсами/"
         "минусами и порекомендуй один с обоснованием."
     )},
    {"name": "optimize", "aliases": ["perf"], "type": "turn", "group": "Эксперт",
     "args": "<код>", "desc": "Узкие места производительности",
     "overlay": (
         "Найди узкие места по производительности в приведённом коде: "
         "алгоритмическая сложность, лишние проходы, аллокации, I/O, N+1. Дай "
         "конкретные улучшения с оценкой эффекта. Не микрооптимизируй там, где это не важно."
     )},
    {"name": "security", "aliases": ["sec"], "type": "turn", "group": "Эксперт",
     "args": "<код|схема>", "desc": "Security-ревью",
     "overlay": (
         "Проведи security-ревью: инъекции (SQL/команд), аутентификация и "
         "авторизация, утечки секретов, небезопасная десериализация, SSRF, XSS, "
         "права доступа, валидация ввода. Для каждой находки — риск и как "
         "исправить. Только реальные риски, без театра."
     )},
    {"name": "test", "aliases": ["tests"], "type": "turn", "group": "Эксперт",
     "args": "<код|функция>", "desc": "Предложить тесты",
     "overlay": (
         "Предложи тесты для приведённого кода/функции: счастливый путь, "
         "граничные случаи, ошибки и исключения. Дай готовый код тестов под стек "
         "проекта; если стек неясен — спроси, какой использовать."
     )},
    {"name": "refactor", "aliases": ["rf"], "type": "turn", "group": "Эксперт",
     "args": "<код>", "desc": "Упростить без смены поведения",
     "overlay": (
         "Упрости и почисти код БЕЗ изменения поведения и внешнего контракта: "
         "убери дубли, проясни имена, разбей длинное, сократи вложенность. Покажи "
         "итог и кратко перечисли, что изменил и почему."
     )},
    {"name": "eli5", "aliases": ["simple"], "type": "turn", "group": "Эксперт",
     "args": "<тема>", "desc": "Объяснить как ребёнку",
     "overlay": (
         "Объясни максимально просто, как другу без технического бэкграунда: "
         "простыми словами, с бытовой аналогией, без жаргона и формул."
     )},
    {"name": "critique", "aliases": ["roast"], "type": "turn", "group": "Эксперт",
     "args": "<идея|текст>", "desc": "Жёсткая честная критика",
     "overlay": (
         "Включи режим жёсткого, но честного критика: укажи слабые места, риски "
         "и дыры прямо и первым делом, с конкретикой и альтернативой «как лучше». "
         "Без лести и дежурных похвал. Цель — сделать лучше, а не понравиться."
     )},
    {"name": "summarize", "aliases": ["tldr", "sum"], "type": "turn", "group": "Эксперт",
     "args": "<текст>", "desc": "Краткое саммари",
     "overlay": (
         "Сделай краткое и точное саммари приведённого текста: ключевые мысли "
         "списком, без воды. Не добавляй того, чего в тексте нет. Если текст "
         "длинный — сначала 1–2 строки сути, потом пункты."
     )},
    {"name": "rewrite", "aliases": ["edit-text"], "type": "turn", "group": "Эксперт",
     "args": "<текст>", "desc": "Переписать яснее",
     "overlay": (
         "Перепиши приведённый текст: яснее, чище, тот же смысл и тот же язык "
         "оригинала. Если стиль не указан — сделай нейтрально-человечный, без "
         "канцелярита. Верни только переписанный вариант."
     )},
    {"name": "translate", "aliases": ["tr"], "type": "turn", "group": "Эксперт",
     "args": "<текст>", "desc": "Перевести (ru↔en)",
     "overlay": (
         "Переведи приведённый текст. Если язык назначения не указан: русский → "
         "на английский, иначе → на русский. Сохрани смысл и тон. Верни только "
         "перевод, без пояснений."
     )},
    {"name": "proscons", "aliases": ["pros-cons"], "type": "turn", "group": "Эксперт",
     "args": "<вопрос>", "desc": "За и против + вывод",
     "overlay": (
         "Разбери вопрос как «за и против»: честный список плюсов и минусов "
         "(без перекоса), затем краткий вывод-рекомендация с обоснованием. Если "
         "вариантов больше двух — сравни их по ключевым критериям."
     )},
    {"name": "steps", "aliases": ["howto"], "type": "turn", "group": "Эксперт",
     "args": "<задача>", "desc": "Чёткая инструкция по шагам",
     "overlay": (
         "Дай чёткую пошаговую инструкцию: пронумерованные шаги, каждый — одно "
         "конкретное действие, по порядку. Без лишней теории. В конце — как "
         "проверить, что всё получилось."
     )},
    # ── Помощь ───────────────────────────────────────────────────────────
    {"name": "help", "aliases": ["commands", "?"], "type": "client", "group": "Помощь",
     "args": "", "desc": "Список всех команд"},
]

# name|alias → spec
_BY_TOKEN: dict[str, dict[str, Any]] = {}
for _spec in COMMAND_SPECS:
    _BY_TOKEN[_spec["name"]] = _spec
    for _al in _spec["aliases"]:
        _BY_TOKEN[_al] = _spec

# Режим/эффорт-директивы для turn-команд → (поле, значение).
_MODE_CMDS = {"plan": "plan", "ask": "ask", "auto": "auto", "bypass": "bypass"}
_EFFORT_CMDS = {"fast": "fast", "normal": "normal", "deep": "deep"}


def commands_json() -> list[dict[str, Any]]:
    """Сериализуемый реестр для GET /api/chat/commands и палитры.

    ``overlay`` — флаг: turn-команда с экспертной накладкой (клиент тогда шлёт
    имя команды в send-stream, сам текст накладки берётся на сервере)."""
    return [
        {
            "name": s["name"],
            "aliases": s["aliases"],
            "type": s["type"],
            "group": s["group"],
            "args": s["args"],
            "desc": s["desc"],
            "overlay": bool(s.get("overlay")),
        }
        for s in COMMAND_SPECS
    ]


def command_overlay(name: str) -> str | None:
    """Текст экспертной накладки для turn-команды (или None). Сервер-авторитет:
    клиент шлёт только имя, инструкция берётся отсюда."""
    spec = find_command(name)
    if spec is None:
        return None
    ov = spec.get("overlay")
    return ov if isinstance(ov, str) and ov.strip() else None


def split_command(text: str) -> tuple[str, str] | None:
    """`/name arg...` → (name_lower, arg). None — если это не команда.

    `//текст` (двойной слэш) — литерал, командой НЕ считается.
    """
    text = (text or "").lstrip()
    if not text.startswith("/") or text.startswith("//"):
        return None
    body = text[1:]
    if not body or body[0].isspace():
        return None
    parts = body.split(None, 1)
    name = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    return name, arg


def find_command(token: str) -> dict[str, Any] | None:
    return _BY_TOKEN.get((token or "").lower().lstrip("/"))


def expand_command(text: str) -> dict[str, Any] | None:
    """Разобрать команду из текста сообщения.

    Возвращает:
      * None — текст не команда (обычное сообщение, слать как есть);
      * {recognized: False, name} — слэш-токен есть, но команды нет (не съедать);
      * {recognized: True, name, type, arg, force_mode?, force_effort?, send_text}
        — распознанная команда. Для turn-команд ``send_text`` — что отправить
        модели (с учётом директив), ``force_mode``/``force_effort`` — override.
    """
    parsed = split_command(text)
    if parsed is None:
        return None
    name, arg = parsed
    spec = find_command(name)
    if spec is None:
        return {"recognized": False, "name": name}
    out: dict[str, Any] = {
        "recognized": True,
        "name": spec["name"],
        "type": spec["type"],
        "arg": arg,
    }
    if spec["type"] == "turn":
        if spec["name"] in _MODE_CMDS:
            out["force_mode"] = _MODE_CMDS[spec["name"]]
            out["send_text"] = arg
        elif spec["name"] in _EFFORT_CMDS:
            out["force_effort"] = _EFFORT_CMDS[spec["name"]]
            out["send_text"] = arg
        elif spec["name"] == "web":
            out["force_mode"] = "auto"
            out["send_text"] = (
                f"Найди в интернете актуальную информацию и ответь: {arg}" if arg else ""
            )
    return out


__all__ = [
    "COMMAND_SPECS",
    "command_overlay",
    "commands_json",
    "expand_command",
    "find_command",
    "split_command",
]
