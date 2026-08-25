from __future__ import annotations

from app.auth import owner as owner_mod
from app.auth.users import create_user
from app.llm.copilot_stream import stream_copilot
from app.storage.repository import get_kv, get_user_kv, set_kv


def _reset_owner_cache() -> None:
    owner_mod._cache["value"] = None
    owner_mod._cache["checked_at"] = 0.0
    owner_mod._fa_cache["value"] = None
    owner_mod._fa_cache["checked_at"] = 0.0


async def test_copilot_can_enable_allowlisted_setting_without_llm(db) -> None:
    """Владелец переключает свои глобальные флаги фразой в копилоте."""
    owner = await create_user("owner@example.test", "Zq7-frost-lantern-91")
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


async def test_copilot_setting_action_never_touches_global_flags(db) -> None:
    """Обычный пользователь НЕ переключает глобальные флаги владельца.

    ``ai_everywhere`` / ``advanced_mode`` / ``feat_tools`` — общие kv-строки
    инстанса. До гейта любой зарегистрированный аккаунт мог фразой «включи
    инструменты» в копилоте поменять их владельцу.

    Фича у участника при этом ЕСТЬ — роль выбирает адрес записи, а не наличие
    действия: та же фраза пишет ЕГО ``user_settings`` (см. подробные проверки
    в tests/test_copilot_member.py).
    """
    owner = await create_user("owner@example.test", "Zq7-frost-lantern-91")
    member = await create_user("member@example.test", "Kp4-velvet-harbour-38")
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

    # Глобальные флаги владельца не тронуты — это и был исходный баг.
    assert await get_kv(db, "advanced_mode") is None
    assert await get_kv(db, "feat_tools") is None
    # Участнику применилось СВОЁ, и он это видит.
    assert [event["type"] for event in events] == ["meta", "delta", "done"]
    assert events[-1]["href"] == "/settings/advanced"
    assert await get_user_kv(db, int(member["id"]), "feat_tools") == "1"
