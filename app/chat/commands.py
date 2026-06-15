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
    # ── Окна / навигация (client) ────────────────────────────────────────
    {"name": "activity", "aliases": ["acts"], "type": "client", "group": "Окна",
     "args": "", "desc": "Окно «что делает ИИ»"},
    {"name": "voice", "aliases": [], "type": "client", "group": "Окна",
     "args": "", "desc": "Настройки голоса"},
    {"name": "theme", "aliases": [], "type": "client", "group": "Окна",
     "args": "", "desc": "Темы оформления"},
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
    """Сериализуемый реестр для GET /api/chat/commands и палитры."""
    return [
        {
            "name": s["name"],
            "aliases": s["aliases"],
            "type": s["type"],
            "group": s["group"],
            "args": s["args"],
            "desc": s["desc"],
        }
        for s in COMMAND_SPECS
    ]


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
    "commands_json",
    "expand_command",
    "find_command",
    "split_command",
]
