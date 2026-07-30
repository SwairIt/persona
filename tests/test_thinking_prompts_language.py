"""Owner mandate 2026-07-30: qwen2.5:7b defaults to Chinese unless the
prompt explicitly pins the answer language — a Russian-language PROMPT is
not itself an instruction. Every prompt string in ``app.thinking.loop`` must
say so explicitly, asserted programmatically over the whole collection so a
prompt added later without it fails this test rather than silently shipping
Chinese output in production again.
"""

from __future__ import annotations

from app.thinking import loop

_RUSSIAN_MARKERS = ("на русском", "русском языке")


def _all_prompt_strings() -> dict[str, str]:
    prompts = dict(loop.SEED_PROMPTS)
    prompts["_STEP_SYSTEM"] = loop._STEP_SYSTEM
    prompts["_STEP_SYSTEM_MODEL_MODE"] = loop._STEP_SYSTEM_MODEL_MODE
    prompts["_CONCLUSION_SYSTEM"] = loop._CONCLUSION_SYSTEM
    return prompts


def test_every_prompt_explicitly_requires_russian() -> None:
    offenders = [
        name
        for name, text in _all_prompt_strings().items()
        if not any(marker in text for marker in _RUSSIAN_MARKERS)
    ]
    assert offenders == [], (
        "these prompts never instruct the model to answer in Russian: "
        + ", ".join(offenders)
    )


def test_evidence_dependent_prompts_state_the_owner_identity_rule() -> None:
    """know_you/self_check/unfinished quote real evidence that may mention
    the owner by name — they must say plainly that a named mention is the
    same person, not a third party (the exact bug observed live: the model
    split "владелец" and "Ярослав" into two people)."""
    identity_markers = ("тот же самый человек", "не третье лицо")
    for kind in ("know_you", "self_check", "unfinished"):
        text = loop.SEED_PROMPTS[kind]
        assert any(marker in text for marker in identity_markers), (
            f"SEED_PROMPTS[{kind!r}] must state the named-mention == owner rule"
        )
    for name in ("_STEP_SYSTEM", "_STEP_SYSTEM_MODEL_MODE", "_CONCLUSION_SYSTEM"):
        text = getattr(loop, name)
        assert any(marker in text for marker in identity_markers), (
            f"loop.{name} must state the named-mention == owner rule"
        )
