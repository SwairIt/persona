"""Импорт Markdown-заметок в память (ROADMAP S4a-2)."""

from __future__ import annotations

import aiosqlite
import pytest

from app.chat.markdown_import import import_markdown, parse_markdown_notes

_MD = """# Обо мне
- люблю **кофе** без сахара
- [ ] починить кран
- [x] оплатил интернет

## Работа
Веду проект [Persona](http://example.com).
```
secret_code = should_be_skipped
```
---
| a | b |
| - | - |
ok
"""


def test_parser_basic() -> None:
    facts = parse_markdown_notes(_MD)
    assert "Обо мне: люблю кофе без сахара" in facts  # жирный снят, заголовок-контекст
    assert "Обо мне: починить кран" in facts  # чекбокс снят
    assert any("оплатил интернет" in f for f in facts)
    assert any("Веду проект Persona" in f for f in facts)  # ссылка → текст


def test_parser_skips_code_and_separators() -> None:
    facts = parse_markdown_notes(_MD)
    assert not any("should_be_skipped" in f for f in facts)  # кодоблок
    assert not any(set(f) <= {"-", "|", " ", ":"} for f in facts)  # таблица-сеп/hr


def test_parser_dedup_within_doc() -> None:
    md = "- одно и то же\n- одно и то же\n- другое тут"
    facts = parse_markdown_notes(md)
    assert facts.count("одно и то же") == 1
    assert "другое тут" in facts


def test_parser_max_facts() -> None:
    md = "\n".join(f"- факт номер {i}" for i in range(50))
    assert len(parse_markdown_notes(md, max_facts=10)) == 10


@pytest.mark.asyncio
async def test_import_stores_and_dedups(db: aiosqlite.Connection) -> None:
    await db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'a@b.c','x')")
    await db.commit()

    stats = await import_markdown(1, _MD)
    assert stats["parsed"] > 0
    assert stats["added"] == stats["parsed"]  # всё новое

    from app.chat.user_memory import list_memory

    mem = await list_memory(1, limit=100)
    assert any("кофе без сахара" in m["text"] for m in mem)

    # повторный импорт — все дубли, ничего не добавилось
    stats2 = await import_markdown(1, _MD)
    assert stats2["added"] == 0
    assert stats2["duplicates"] == stats2["parsed"]
