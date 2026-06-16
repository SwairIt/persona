"""Тесты извлечения фактов: GBNF-схема (Ollama) + строковый fallback (ROADMAP S1b)."""

from __future__ import annotations

import pytest

from app.chat.user_memory import _extract_facts


class _FakeJson:
    """Имитация OllamaClient: есть complete_json (GBNF-путь)."""
    async def complete_json(self, request, schema):  # noqa: ANN001, ARG002
        return {"facts": [
            {"text": "любит кофе по утрам", "kind": "preference"},
            {"text": "живёт в Берлине", "kind": "fact"},
            {"text": "", "kind": "fact"},  # пустой — отфильтровать
        ]}

    async def complete(self, request):  # noqa: ANN001, ARG002
        raise AssertionError("не должно вызываться при наличии complete_json")


class _FakeStr:
    """Имитация облачного клиента: только complete (строковый парсер)."""
    async def complete(self, request):  # noqa: ANN001, ARG002
        return "- работает программистом\n- НЕТ\n- увлекается бегом\n  \n- x"


class _FakeJsonBroken:
    """complete_json падает → fallback на строковый complete."""
    async def complete_json(self, request, schema):  # noqa: ANN001, ARG002
        raise ValueError("schema decode failed")

    async def complete(self, request):  # noqa: ANN001, ARG002
        return "- запасной факт из fallback"


@pytest.mark.asyncio
async def test_extract_facts_gbnf_path() -> None:
    facts = await _extract_facts(_FakeJson(), "sys", "user")
    texts = [f["text"] for f in facts]
    assert texts == ["любит кофе по утрам", "живёт в Берлине"]  # пустой отфильтрован
    assert facts[0]["kind"] == "preference"


@pytest.mark.asyncio
async def test_extract_facts_string_fallback() -> None:
    facts = await _extract_facts(_FakeStr(), "sys", "user")
    texts = [f["text"] for f in facts]
    # «НЕТ», пустые и слишком короткие («x») отфильтрованы
    assert texts == ["работает программистом", "увлекается бегом"]
    assert all(f["kind"] == "fact" for f in facts)


@pytest.mark.asyncio
async def test_extract_facts_broken_json_falls_back() -> None:
    facts = await _extract_facts(_FakeJsonBroken(), "sys", "user")
    assert [f["text"] for f in facts] == ["запасной факт из fallback"]
