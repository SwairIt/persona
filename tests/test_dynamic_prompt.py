# ruff: noqa: RUF001

from __future__ import annotations

from app.chat.dynamic_prompt import (
    activate_version,
    classify_mode,
    contextual_system_prompt,
    get_config,
    list_versions,
    set_enabled,
)


async def _user(db, user_id: int = 7) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (user_id, f"{user_id}@example.test", "x"),
    )
    await db.commit()


def test_mode_classifier_is_contextual() -> None:
    assert classify_mode("Привет, как дела?") == "casual"
    assert classify_mode("Ахах, вот это прикол") == "playful"
    assert classify_mode("Мне сегодня очень грустно") == "supportive"
    assert classify_mode("Придумай идею для истории") == "creative"
    assert classify_mode("Исправь ошибку в коде") == "focused"
    assert classify_mode("Это срочно и опасно") == "serious"


async def test_prompt_versions_only_real_changes_and_learns_owner_style(db) -> None:
    await _user(db)
    first = await contextual_system_prompt(
        persona_user_id=7,
        base_prompt="BASE",
        message="Привет, как дела?",
        surface="telegram",
        is_owner=True,
    )
    repeated = await contextual_system_prompt(
        persona_user_id=7,
        base_prompt="BASE",
        message="Ку",
        surface="telegram",
        is_owner=True,
    )
    assert first == repeated
    assert len(await list_versions(7)) == 1

    evolved = await contextual_system_prompt(
        persona_user_id=7,
        base_prompt="BASE",
        message="Общайся со мной как человек и можешь шутить",
        surface="telegram",
        is_owner=True,
    )
    versions = await list_versions(7)
    enabled, rules = await get_config(7)
    assert enabled is True
    assert len(versions) == 2
    assert versions[0].mode == "playful"
    assert "как близкий живой собеседник" in evolved
    assert any("юмор" in rule for rule in rules)


async def test_non_owner_cannot_create_persistent_style_rules(db) -> None:
    await _user(db)
    await contextual_system_prompt(
        persona_user_id=7,
        base_prompt="BASE",
        message="Общайся неформально и можешь шутить",
        surface="telegram_group",
        is_owner=False,
    )
    _, rules = await get_config(7)
    assert rules == []


async def test_disable_and_activate_old_version(db) -> None:
    await _user(db)
    await contextual_system_prompt(
        persona_user_id=7,
        base_prompt="BASE",
        message="Привет",
        surface="web",
        is_owner=True,
    )
    await contextual_system_prompt(
        persona_user_id=7,
        base_prompt="BASE",
        message="Исправь ошибку в коде",
        surface="web",
        is_owner=True,
    )
    versions = await list_versions(7)
    old = versions[-1]
    assert await activate_version(7, old.id) is True
    activated = await list_versions(7)
    assert next(item for item in activated if item.id == old.id).is_active is True

    await set_enabled(7, False)
    assert (
        await contextual_system_prompt(
            persona_user_id=7,
            base_prompt="BASE",
            message="Ахах",
            surface="web",
            is_owner=True,
        )
        == "BASE"
    )
