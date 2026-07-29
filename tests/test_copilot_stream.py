from __future__ import annotations

from app.llm.copilot_stream import stream_copilot
from app.storage.repository import get_kv


async def test_copilot_can_enable_allowlisted_setting_without_llm(db) -> None:
    events = [
        event
        async for event in stream_copilot(
            "включи инструменты",
            page_url="/settings",
            user_id=1,
        )
    ]

    assert [event["type"] for event in events] == ["meta", "delta", "done"]
    assert events[-1]["href"] == "/settings/advanced"
    assert await get_kv(db, "advanced_mode") == "1"
    assert await get_kv(db, "feat_tools") == "1"
