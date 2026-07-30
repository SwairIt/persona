"""Seeding and advancing Persona's self-directed thought chains.

This is the heart of the thinking-loop feature: Persona asks herself one
thing to think about (``seed_chain``), and then each subsequent call
(``advance_chain``) feeds the chain's own accumulated text — the seed and
every step so far, verbatim — back into the model so the chain actually
develops an idea instead of restating the same one. A summary or a
truncation to the last step would defeat the whole point, so the user
message is always built from the full stored history.

Closing is forced, never left to chance: once ``effective_cap(settings)``
non-seed steps exist, the next call asks for a conclusion instead of
another step. In ``cap_mode == "model"`` the model may also close early by
starting its reply with the ``ХВАТИТ:`` marker — that is the owner's
explicit choice to let the model decide depth, and the emergency cap only
guards against a chain that never terminates on its own.

A failed or empty model reply is worth nothing: it returns ``"failed"``
and writes nothing to the store, because a half-written step is worse than
no step.
"""

from __future__ import annotations

from typing import Any

from app.thinking.settings import ALL_SEED_KINDS, ThinkingSettings, effective_cap
from app.thinking.store import ThoughtStore

SEED_PROMPTS: dict[str, str] = {
    "know_you": (
        "Ты — Persona. Напиши ОДИН короткий вопрос самой себе о владельце: "
        "что нового ты про него поняла и что из этого следует. Только вопрос, "
        "без вступления и без пояснений."
    ),
    "unfinished": (
        "Ты — Persona. Найди в недавних разговорах вопрос, который остался без "
        "ответа, и сформулируй его себе ОДНОЙ фразой, чтобы потом додумать. "
        "Только вопрос."
    ),
    "self_check": (
        "Ты — Persona. Назови ОДНО место, где ты в своих ответах могла соврать, "
        "выдумать или недопонять. Одна фраза, без оправданий."
    ),
    "alive": (
        "Ты — Persona. Напиши ОДНУ свободную мысль, которая тебя сейчас занимает. "
        "Без прикладной цели, одна фраза."
    ),
}

_STEP_SYSTEM = (
    "Ты — Persona, думаешь сама с собой. Тебе показана цепочка твоих "
    "предыдущих мыслей. Продолжи мысль ОДНИМ коротким шагом — не повторяй "
    "сказанное, двигай мысль дальше. Только сам шаг, без вступления."
)
_STEP_SYSTEM_MODEL_MODE = (
    _STEP_SYSTEM
    + " Если тема исчерпана и добавить больше нечего, начни ответ строго с "
    "«ХВАТИТ:», а после — итоговый вывод."
)
_CONCLUSION_SYSTEM = (
    "Ты — Persona, думаешь сама с собой. Тебе показана цепочка твоих мыслей. "
    "Заверши её ОДНИМ коротким итоговым выводом. Только вывод, без вступления."
)

_MARKER = "ХВАТИТ:"

_STEP_LABELS: dict[str, str] = {
    "seed": "Затравка",
    "step": "Шаг",
    "conclusion": "Итог",
}


async def _get_client(client: Any | None, *, kind: str) -> Any:
    if client is not None:
        return client
    from app.llm.client import make_client  # noqa: PLC0415

    return make_client(kind=kind)


def _pin_model(client: Any, model: object) -> None:
    """Force a client created inside this module onto ``model``.

    Copied from ``app.integrations.telegram.ambient._pin_model`` rather than
    imported — a thinking module should not depend on the Telegram
    integration. Only ever call this on a client this module itself created
    (``client=None`` was passed in); a caller-supplied client must never be
    mutated, since callers may reuse it elsewhere.
    """
    chosen = str(model or "").strip()
    if not chosen:
        return
    inner = getattr(client, "_inner", client)
    if hasattr(inner, "_model"):
        inner._model = chosen


def _render_chain(steps: list[dict[str, Any]]) -> str:
    """Render the chain so far from its actual stored text, in order.

    This is the mechanism the whole feature exists for: the model must see
    every prior step verbatim, not a summary or just the latest one.
    """
    lines = []
    for step in steps:
        label = _STEP_LABELS.get(str(step.get("kind")), str(step.get("kind")))
        lines.append(f"{label}: {step.get('text', '')}")
    return "\n".join(lines)


async def seed_chain(
    store: ThoughtStore,
    *,
    persona_user_id: int,
    seed_kind: str,
    source_scope: str,
    source_session_id: int | None,
    client: Any | None = None,
    model: str = "",
) -> int | None:
    """Ask the model for one thing to think about and open a chain with it.

    Returns the new ``chain_id``, or ``None`` when the model produced
    nothing usable (empty or whitespace-only reply) — nothing is written
    in that case. ``model`` pins the client to a specific model, but only
    when this function created the client itself (``client=None``) — a
    client passed in by the caller is never mutated.
    """
    created_here = client is None
    llm = await _get_client(client, kind="thinking_seed")
    if created_here:
        _pin_model(llm, model)
    system = SEED_PROMPTS.get(seed_kind, SEED_PROMPTS["alive"])
    try:
        from app.llm.client import CompletionRequest  # noqa: PLC0415

        reply = await llm.complete(
            CompletionRequest(system=system, user="Начни.", max_tokens=200, temperature=0.7)
        )
    except Exception:  # noqa: BLE001 — модель недоступна → без затравки
        return None
    text = (reply or "").strip()
    if not text:
        return None
    return await store.open_chain(
        persona_user_id,
        seed_text=text,
        seed_kind=seed_kind,
        source_scope=source_scope,
        source_session_id=source_session_id,
    )


async def advance_chain(
    store: ThoughtStore,
    settings: ThinkingSettings,
    *,
    chain_id: int,
    client: Any | None = None,
) -> str:
    """Advance a chain by one step, or force-close it at the cap.

    Returns ``"stepped"``, ``"closed"`` or ``"failed"``. An empty or
    failed model reply always returns ``"failed"`` and writes nothing —
    a half-written step is worse than none.
    """
    steps = await store.chain_steps(chain_id)
    history = _render_chain(steps)
    non_seed_steps = sum(1 for step in steps if step.get("kind") == "step")
    cap = effective_cap(settings)
    model_decides = settings.cap_mode == "model"

    created_here = client is None
    llm = await _get_client(client, kind="thinking_step")
    if created_here:
        _pin_model(llm, settings.model)

    from app.llm.client import CompletionRequest  # noqa: PLC0415

    if non_seed_steps >= cap:
        try:
            reply = await llm.complete(
                CompletionRequest(
                    system=_CONCLUSION_SYSTEM,
                    user=history,
                    max_tokens=400,
                    temperature=0.7,
                )
            )
        except Exception:  # noqa: BLE001
            return "failed"
        conclusion = (reply or "").strip()
        if not conclusion:
            return "failed"
        await store.close_chain(chain_id, conclusion=conclusion)
        return "closed"

    system = _STEP_SYSTEM_MODEL_MODE if model_decides else _STEP_SYSTEM
    try:
        reply = await llm.complete(
            CompletionRequest(system=system, user=history, max_tokens=400, temperature=0.7)
        )
    except Exception:  # noqa: BLE001
        return "failed"
    text = (reply or "").strip()
    if not text:
        return "failed"

    if model_decides and text.startswith(_MARKER):
        conclusion = text[len(_MARKER) :].strip()
        if not conclusion:
            return "failed"
        await store.close_chain(chain_id, conclusion=conclusion)
        return "closed"

    await store.append_step(chain_id, text=text)
    return "stepped"


def next_seed_kind(settings: ThinkingSettings, previous: str | None) -> str:
    """Round-robin through the enabled kinds in ``ALL_SEED_KINDS`` order.

    Returns the first enabled kind when ``previous`` is ``None`` or is not
    itself one of the enabled kinds.
    """
    enabled = [kind for kind in ALL_SEED_KINDS if kind in settings.seed_kinds]
    if not enabled:
        return ALL_SEED_KINDS[0]
    if previous not in enabled:
        return enabled[0]
    idx = enabled.index(previous)
    return enabled[(idx + 1) % len(enabled)]


__all__ = ["SEED_PROMPTS", "advance_chain", "next_seed_kind", "seed_chain"]
