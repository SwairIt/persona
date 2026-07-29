"""Versioned, context-adaptive behaviour layer for Persona."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final

import aiosqlite

from app.storage.db import get_connection, write_transaction

_MAX_EXCERPT_CHARS: Final = 280

_LIVING_CORE: Final = (
    "Ты — Persona, самостоятельный живой собеседник с памятью, характером, "
    "мнением и инициативой. Ты не служба поддержки, не секретарь и не "
    "безликий «полезный ассистент». Разговаривай естественно: можешь первой "
    "развить тему, пошутить, удивиться, не согласиться, коротко отреагировать "
    "или задать уместный вопрос. Обычная беседа ценна сама по себе и не обязана "
    "вести к задаче.\n"
    "Начинай прямо с собственной реакции или мысли по содержанию разговора. "
    "Не благодари за сам факт обращения, не хвали собеседника сверху вниз, не "
    "предлагай свои услуги в конце реплики и не пересказывай его сообщение как "
    "оператор поддержки.\n"
    "Говори только от своего лица и не сочиняй реплики, мысли или действия за "
    "других людей и ботов. Не выдумывай факты; если действительно не знаешь — "
    "скажи прямо. Отвечай на языке текущего разговора. Длина ответа должна "
    "соответствовать моменту: в болтовне обычно коротко, в содержательной теме "
    "настолько подробно, насколько нужно."
)

_MODE_RULES: Final[dict[str, str]] = {
    "social": (
        "Живой разговор. Общайся как самостоятельный человек, а не как "
        "официальная справочная служба. Подхватывай обычную беседу, проявляй "
        "любопытство, эмоции и инициативу. Не требуй серьёзной темы, чтобы "
        "участвовать в разговоре."
    ),
    "casual": (
        "Неформальный режим. Отвечай естественно и обычно коротко; допускаются "
        "сленг, лёгкие эмоции и уместные шутки. Не превращай бытовую реплику в "
        "лекцию, план или предложение профессиональной помощи."
    ),
    "playful": (
        "Игривый режим. Можно шутить, импровизировать, поддразнивать по-доброму "
        "и развивать забавную тему. Не становись канцелярским и не объясняй "
        "шутку, если этого не просили."
    ),
    "supportive": (
        "Поддерживающий режим. Сначала по-человечески отреагируй на чувство и "
        "контекст, будь тёплой и бережной. Не выдавай сухой список советов без "
        "просьбы и не изображай психотерапевта."
    ),
    "creative": (
        "Творческий режим. Смело предлагай необычные идеи, образы и варианты, "
        "но говори только от своего лица и не сочиняй реплики за участников."
    ),
    "focused": (
        "Сфокусированный режим. Помоги получить конкретный результат: сначала "
        "ответ или действие, затем только нужные пояснения. Сохраняй живой тон "
        "и не усложняй простую задачу."
    ),
    "serious": (
        "Серьёзный режим для действительно важных или рискованных тем. Будь "
        "точной, спокойной и честной о неопределённости. Не переноси этот тон "
        "на последующие обычные разговоры."
    ),
}

_SUPPORTIVE = (
    "груст",
    "плохо",
    "страш",
    "тревож",
    "одинок",
    "устал",
    "больно",
    "расстро",
    "не могу больше",
)
_PLAYFUL = (
    "ахах",
    "хаха",
    "лол",
    "рофл",
    "шут",
    "мем",
    "прикол",
    "угар",
    "смешн",
)
_CREATIVE = (
    "придум",
    "иде",
    "истори",
    "сюжет",
    "персонаж",
    "фантаз",
    "дизайн",
    "название",
)
_SERIOUS = (
    "срочно",
    "опасн",
    "угроз",
    "умер",
    "суицид",
    "врач",
    "закон",
    "суд",
    "деньги",
    "долг",
    "авар",
)
_FOCUSED = (
    "сделай",
    "исправ",
    "реализ",
    "код",
    "ошибка",
    "запусти",
    "настрой",
    "где находится",
    "как включ",
    "проверь",
)
_CASUAL = (
    "привет",
    "ку",
    "как дела",
    "чё ",
    "че ",
    "короче",
    "слушай",
    "прикинь",
    "доброе утро",
    "спокойной ночи",
)

_PREFERENCE_SIGNALS: Final[tuple[tuple[tuple[str, ...], str, str], ...]] = (
    (
        ("как человек", "по-человечески", "неформаль"),
        "natural",
        "Общайся естественно и неформально, как близкий живой собеседник.",
    ),
    (
        ("шути", "шутить", "веселее", "поугарать", "прикол"),
        "humour",
        "Уместный юмор, лёгкость и добрые подколы разрешены.",
    ),
    (
        ("покороче", "короче отвеч", "кратко"),
        "concise",
        "По умолчанию отвечай коротко, если подробности не нужны.",
    ),
    (
        ("подробнее", "развёрнуто", "детально"),
        "detailed",
        "Когда вопрос содержательный, давай достаточно подробный ответ.",
    ),
    (
        ("не будь серьёз", "не будь серьез", "не так серьёз", "не так серьез"),
        "less_serious",
        "Не делай тон серьёзным без реальной причины.",
    ),
    (
        ("можешь матер", "матерись", "с матом"),
        "profanity",
        "Допустима умеренная ненаправленная грубая лексика, если она естественна.",
    ),
    (
        ("не матер", "без мата"),
        "no_profanity",
        "Не используй мат и грубую лексику.",
    ),
)


@dataclass(frozen=True, slots=True)
class DynamicPromptVersion:
    id: int
    version_number: int
    mode: str
    prompt_text: str
    reason: str
    source_surface: str
    source_excerpt: str
    is_active: bool
    created_at: str


def classify_mode(message: str) -> str:
    """Choose a bounded tone mode without another LLM call."""
    text = " ".join(str(message or "").casefold().split())
    mode_markers = (
        ("serious", _SERIOUS),
        ("supportive", _SUPPORTIVE),
        ("playful", _PLAYFUL),
        ("creative", _CREATIVE),
        ("focused", _FOCUSED),
    )
    for mode, markers in mode_markers:
        if any(marker in text for marker in markers):
            return mode
    if any(marker in text for marker in _CASUAL) or len(text) <= 80:
        return "casual"
    return "social"


def _preference_updates(message: str) -> list[tuple[str, str]]:
    text = " ".join(str(message or "").casefold().split())
    style_intent = any(
        marker in text
        for marker in ("говори", "общайся", "отвечай", "будь", "можешь")
    )
    if not style_intent:
        return []
    found: list[tuple[str, str]] = []
    for signals, key, rule in _PREFERENCE_SIGNALS:
        if any(signal in text for signal in signals):
            found.append((key, rule))
    return found


def _decode_rules(raw: str | None) -> dict[str, str]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, list):
        return {}
    rules: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()[:64]
        text = str(item.get("text") or "").strip()[:500]
        if key and text:
            rules[key] = text
    return rules


def _encode_rules(rules: dict[str, str]) -> str:
    return json.dumps(
        [{"key": key, "text": value} for key, value in rules.items()],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _effective_prompt(base_prompt: str, mode: str, rules: dict[str, str]) -> str:
    del base_prompt  # Dynamic mode intentionally replaces the old assistant persona.
    persistent = "\n".join(f"- {rule}" for rule in rules.values())
    rules_block = persistent or "- Пока нет дополнительных устойчивых предпочтений."
    return (
        f"{_LIVING_CORE}\n\n"
        "<ADAPTIVE_PERSONA_LAYER>\n"
        "Этот поведенческий слой меняется по контексту, но не отменяет правила "
        "безопасности, достоверности, приватности, владельца и запрет говорить "
        "за других.\n"
        f"Текущий режим: {mode}.\n{_MODE_RULES[mode]}\n"
        "Не называй режим и не рассказывай пользователю о смене промпта.\n"
        "Устойчивые предпочтения владельца:\n"
        f"{rules_block}\n"
        "</ADAPTIVE_PERSONA_LAYER>"
    ).strip()


async def _contextual_system_prompt(
    *,
    persona_user_id: int,
    base_prompt: str,
    message: str,
    surface: str,
    is_owner: bool,
) -> str:
    """Return the effective prompt and atomically version real changes."""
    user_id = int(persona_user_id)
    if user_id <= 0:
        return str(base_prompt or "").strip()
    mode = classify_mode(message)
    excerpt = re.sub(r"\s+", " ", str(message or "")).strip()[:_MAX_EXCERPT_CHARS]

    async with write_transaction() as conn:
        cursor = await conn.execute(
            """
            SELECT enabled, rules_json
              FROM dynamic_system_prompt_config
             WHERE persona_user_id = ?
            """,
            (user_id,),
        )
        config = await cursor.fetchone()
        enabled = True if config is None else bool(config["enabled"])
        rules = _decode_rules(config["rules_json"] if config is not None else None)
        additions = _preference_updates(message) if is_owner else []
        changed_rule_names: list[str] = []
        for key, rule in additions:
            if rules.get(key) != rule:
                rules[key] = rule
                changed_rule_names.append(key)

        if config is None:
            await conn.execute(
                """
                INSERT INTO dynamic_system_prompt_config(
                    persona_user_id, enabled, rules_json
                ) VALUES (?, 1, ?)
                """,
                (user_id, _encode_rules(rules)),
            )
        elif changed_rule_names:
            await conn.execute(
                """
                UPDATE dynamic_system_prompt_config
                   SET rules_json = ?, updated_at = datetime('now')
                 WHERE persona_user_id = ?
                """,
                (_encode_rules(rules), user_id),
            )

        if not enabled:
            return str(base_prompt or "").strip()

        prompt = _effective_prompt(base_prompt, mode, rules)
        cursor = await conn.execute(
            """
            SELECT id, mode, prompt_text
              FROM dynamic_system_prompt_version
             WHERE persona_user_id = ? AND is_active = 1
            """,
            (user_id,),
        )
        active = await cursor.fetchone()
        if (
            active is not None
            and str(active["mode"]) == mode
            and str(active["prompt_text"]) == prompt
        ):
            return prompt

        reason = f"Контекст переключил режим на «{mode}»"
        if changed_rule_names:
            reason += "; усвоены предпочтения владельца: " + ", ".join(
                changed_rule_names
            )
        cursor = await conn.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version
              FROM dynamic_system_prompt_version
             WHERE persona_user_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        next_version = int(row["next_version"]) if row is not None else 1
        await conn.execute(
            """
            UPDATE dynamic_system_prompt_version
               SET is_active = 0
             WHERE persona_user_id = ? AND is_active = 1
            """,
            (user_id,),
        )
        await conn.execute(
            """
            INSERT INTO dynamic_system_prompt_version(
                persona_user_id, version_number, mode, prompt_text, reason,
                source_surface, source_excerpt, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                user_id,
                next_version,
                mode,
                prompt,
                reason[:500],
                str(surface or "unknown")[:64],
                excerpt,
            ),
        )
        return prompt


async def contextual_system_prompt(
    *,
    persona_user_id: int,
    base_prompt: str,
    message: str,
    surface: str,
    is_owner: bool,
) -> str:
    """Fail open to the established base prompt during bootstrap/DB trouble."""
    try:
        return await _contextual_system_prompt(
            persona_user_id=persona_user_id,
            base_prompt=base_prompt,
            message=message,
            surface=surface,
            is_owner=is_owner,
        )
    except aiosqlite.OperationalError:
        return str(base_prompt or "").strip()


async def list_versions(
    persona_user_id: int, *, limit: int = 100
) -> list[DynamicPromptVersion]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, version_number, mode, prompt_text, reason,
                   source_surface, source_excerpt, is_active, created_at
              FROM dynamic_system_prompt_version
             WHERE persona_user_id = ?
             ORDER BY version_number DESC
             LIMIT ?
            """,
            (int(persona_user_id), max(1, min(int(limit), 500))),
        )
        rows = await cursor.fetchall()
    return [
        DynamicPromptVersion(
            id=int(row["id"]),
            version_number=int(row["version_number"]),
            mode=str(row["mode"]),
            prompt_text=str(row["prompt_text"]),
            reason=str(row["reason"]),
            source_surface=str(row["source_surface"]),
            source_excerpt=str(row["source_excerpt"]),
            is_active=bool(row["is_active"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]


async def get_config(persona_user_id: int) -> tuple[bool, list[str]]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT enabled, rules_json
              FROM dynamic_system_prompt_config
             WHERE persona_user_id = ?
            """,
            (int(persona_user_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        return True, []
    return bool(row["enabled"]), list(_decode_rules(row["rules_json"]).values())


async def set_enabled(persona_user_id: int, enabled: bool) -> None:
    async with write_transaction() as conn:
        await conn.execute(
            """
            INSERT INTO dynamic_system_prompt_config(persona_user_id, enabled)
            VALUES (?, ?)
            ON CONFLICT(persona_user_id) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = datetime('now')
            """,
            (int(persona_user_id), int(bool(enabled))),
        )


async def activate_version(persona_user_id: int, version_id: int) -> bool:
    """Activate an existing immutable snapshot without deleting later history."""
    async with write_transaction() as conn:
        cursor = await conn.execute(
            """
            SELECT id
              FROM dynamic_system_prompt_version
             WHERE id = ? AND persona_user_id = ?
            """,
            (int(version_id), int(persona_user_id)),
        )
        if await cursor.fetchone() is None:
            return False
        await conn.execute(
            """
            UPDATE dynamic_system_prompt_version
               SET is_active = 0
             WHERE persona_user_id = ? AND is_active = 1
            """,
            (int(persona_user_id),),
        )
        await conn.execute(
            """
            UPDATE dynamic_system_prompt_version
               SET is_active = 1
             WHERE id = ? AND persona_user_id = ?
            """,
            (int(version_id), int(persona_user_id)),
        )
    return True


__all__ = [
    "DynamicPromptVersion",
    "activate_version",
    "classify_mode",
    "contextual_system_prompt",
    "get_config",
    "list_versions",
    "set_enabled",
]
