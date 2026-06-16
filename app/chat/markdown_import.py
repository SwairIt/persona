"""Импорт Markdown-заметок в память (ROADMAP S4a-2).

Пользователь вставляет/загружает свои .md-заметки (Obsidian, Notion-экспорт,
просто файл) → разбираем на отдельные факты → кладём в user_memory. Local-first
способ «подружить» ассистента со своей базой знаний без облака.

Парсер детерминированный (без LLM, покрыт тестами): пункты списков, чекбоксы,
параграфы становятся фактами; заголовки служат КОНТЕКСТОМ и префиксят факты под
ними («Раздел: пункт»), чтобы факт не терял смысла вне документа. Инлайн-разметка
(жирный/курсив/код/ссылки) очищается; кодоблоки, разделители и таблицы-сепараторы
пропускаются. Дедуп — на уровне add_memory (точное совпадение текста).
"""

from __future__ import annotations

import re

_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_CHECKBOX = re.compile(r"^\[[ xX]\]\s*")
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.*)$")
_BLOCKQUOTE = re.compile(r"^\s*>\s?")
_HR = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|\-]+\|?\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")

# Инлайн-разметка → чистый текст.
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_IMG = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_BOLD2 = re.compile(r"__(.+?)__")
_ITALIC = re.compile(r"(?<!\w)\*(.+?)\*(?!\w)")
_ITALIC2 = re.compile(r"(?<!\w)_(.+?)_(?!\w)")
_CODE = re.compile(r"`([^`]+)`")

_MIN_LEN = 6
_DEFAULT_MAX = 200


def _clean_inline(text: str) -> str:
    """Снять инлайн-разметку Markdown, вернуть человеческий текст."""
    text = _IMG.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    text = _BOLD2.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    text = _ITALIC2.sub(r"\1", text)
    text = _CODE.sub(r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def parse_markdown_notes(md: str, *, max_facts: int = _DEFAULT_MAX) -> list[str]:
    """Markdown → список фактов. Детерминированно, без дублей внутри документа."""
    facts: list[str] = []
    seen: set[str] = set()
    section = ""
    in_fence = False

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.strip():
            continue
        if _HR.match(line) or _TABLE_SEP.match(line):
            continue

        mh = _HEADING.match(line)
        if mh:
            section = _clean_inline(mh.group(2))
            continue

        # снять blockquote / маркер списка / чекбокс
        body = _BLOCKQUOTE.sub("", line)
        body = _LIST_MARKER.sub("", body)
        body = _CHECKBOX.sub("", body)
        body = _clean_inline(body)
        if len(body) < _MIN_LEN:
            continue

        fact = f"{section}: {body}" if section else body
        key = fact.lower()
        if key in seen:
            continue
        seen.add(key)
        facts.append(fact)
        if len(facts) >= max_facts:
            break
    return facts


async def import_markdown(user_id: int, md: str, *, max_facts: int = _DEFAULT_MAX) -> dict[str, int]:
    """Разобрать Markdown и сложить факты в user_memory. Возвращает статистику."""
    from app.chat.user_memory import add_memory, count_memory  # noqa: PLC0415

    facts = parse_markdown_notes(md, max_facts=max_facts)
    before = await count_memory(user_id)
    for fact in facts:
        await add_memory(user_id, fact, kind="fact")
    after = await count_memory(user_id)
    added = max(0, after - before)
    return {"parsed": len(facts), "added": added, "duplicates": len(facts) - added}


__all__ = ["parse_markdown_notes", "import_markdown"]
