from __future__ import annotations

from app.auth import owner as owner_mod
from app.auth.users import create_user
from app.llm.copilot_stream import stream_copilot
from app.storage.repository import get_kv, set_kv


def _reset_owner_cache() -> None:
    owner_mod._cache["value"] = None
    owner_mod._cache["checked_at"] = 0.0
    owner_mod._fa_cache["value"] = None
    owner_mod._fa_cache["checked_at"] = 0.0


async def test_copilot_can_enable_allowlisted_setting_without_llm(db) -> None:
    """Владелец переключает свои глобальные флаги фразой в копилоте."""
    owner = await create_user("owner@example.test", "owner-pass-123")
    await set_kv(db, "owner_user_id", str(owner["id"]))
    _reset_owner_cache()

    events = [
        event
        async for event in stream_copilot(
            "включи инструменты",
            page_url="/settings",
            user_id=int(owner["id"]),
        )
    ]

    assert [event["type"] for event in events] == ["meta", "delta", "done"]
    assert events[-1]["href"] == "/settings/advanced"
    assert await get_kv(db, "advanced_mode") == "1"
    assert await get_kv(db, "feat_tools") == "1"


async def test_copilot_setting_action_is_owner_only(db) -> None:
    """Обычный пользователь НЕ переключает глобальные флаги владельца.

    ``ai_everywhere`` / ``advanced_mode`` / ``feat_tools`` — общие kv-строки
    инстанса. До гейта любой зарегистрированный аккаунт мог фразой «включи
    инструменты» в копилоте поменять их владельцу.
    """
    owner = await create_user("owner@example.test", "owner-pass-123")
    member = await create_user("member@example.test", "member-pass-123")
    await set_kv(db, "owner_user_id", str(owner["id"]))
    _reset_owner_cache()

    events = [
        event
        async for event in stream_copilot(
            "включи инструменты",
            page_url="/settings",
            user_id=int(member["id"]),
        )
    ]

    # Настройка не применена: флаги владельца не тронуты.
    assert await get_kv(db, "advanced_mode") is None
    assert await get_kv(db, "feat_tools") is None
    # И это не «тихо ок»: раз своей модели у него нет, копилот честно говорит
    # про /settings/llm, а не делает вид, что что-то включил.
    assert [event["type"] for event in events] == ["meta", "error"]
    assert events[-1]["reason"] == "llm_not_configured"
