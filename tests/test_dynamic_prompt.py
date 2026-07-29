# ruff: noqa: RUF001

from __future__ import annotations

from app.chat.dynamic_prompt import (
    _LIVING_CORE,
    _MODE_RULES,
    _compact_rules,
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
    assert classify_mode("Сыграй роль циничного детектива") == "creative"
    assert classify_mode("Пошли меня уже нормально") == "playful"
    assert classify_mode("Исправь ошибку в коде") == "focused"
    assert classify_mode("Это срочно и опасно") == "serious"


def test_living_prompt_has_no_helpful_service_goal() -> None:
    prompt = (_LIVING_CORE + _MODE_RULES["casual"]).casefold()
    assert "полезн" not in prompt
    assert "профессиональной помощи" not in prompt
    assert "конструктив" not in prompt


def test_living_rule_compiler_resolves_conflicts_and_stays_bounded() -> None:
    old = {
        f"old_{index}": ("длинное правило " * 20) + str(index)
        for index in range(8)
    }
    old["profanity"] = "Можно материться."
    compact = _compact_rules(
        old,
        [
            ("no_profanity", "Не используй мат."),
            ("concise", "Отвечай коротко."),
            ("detailed", "Отвечай подробно, когда это нужно."),
        ],
    )
    assert "profanity" not in compact
    assert compact["no_profanity"] == "Не используй мат."
    assert "concise" not in compact
    assert compact["detailed"] == "Отвечай подробно, когда это нужно."
    assert len(compact) <= 6
    assert sum(len(rule) for rule in compact.values()) <= 700


async def test_compact_telegram_prompt_stays_small(db) -> None:
    await _user(db, 8)
    prompt = await contextual_system_prompt(
        persona_user_id=8,
        base_prompt="BASE",
        message="Пошли меня уже нормально",
        surface="telegram",
        is_owner=True,
        compact=True,
    )
    assert len(prompt) < 1_800
    assert "полезн" not in prompt.casefold()
    assert "первой фразой" in prompt.casefold()
    assert "только на «ты»" in prompt.casefold()


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
