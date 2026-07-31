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

No evidence, no thought (owner mandate 2026-07-30): ``know_you``,
``self_check`` and ``unfinished`` are factual claims about the owner, so
``seed_chain`` refuses to seed them — returns ``None``, calls the model
not at all — unless :func:`app.thinking.evidence.gather_evidence` found
real owner messages or existing memory facts to hand the model as the
ONLY permitted source. ``alive`` is the sole kind exempt from this, because
it is explicitly a free thought that makes no factual claim about anyone.

Every stored row also records whether it is a ``certainty`` of
``'observation'`` (the model pointed at supplied evidence) or ``'guess'``
(it inferred). The model is asked to prefix its reply with ``НАБЛЮДЕНИЕ:``
or ``ДОГАДКА:``; :func:`_split_certainty` parses and strips that marker.
Missing or unparsable markers default to ``'guess'`` — unmarked content is
never silently upgraded to an observation. This whole loop has no write
path into ``user_memory`` or any other memory table; that is a structural
property of this package, not a prompt instruction (see
``tests/test_thinking_no_memory_writes.py``).
"""

from __future__ import annotations

from typing import Any

from app.thinking.evidence import gather_evidence
from app.thinking.research_tools import is_research_tool_allowed
from app.thinking.settings import ALL_SEED_KINDS, ThinkingSettings, effective_cap
from app.thinking.store import ThoughtStore

# Seed kinds that make a factual claim about the owner and therefore may
# never be asked without real evidence to point at. ``alive`` is the only
# kind left out — a free thought, no factual claim, no evidence required.
_EVIDENCE_REQUIRED_KINDS: frozenset[str] = frozenset({"know_you", "self_check", "unfinished"})

# The model backing this loop (e.g. qwen2.5:7b) defaults to Chinese when
# nothing pins the output language — a Russian-language PROMPT is not an
# instruction, the model does not infer "answer in the language I was asked
# in". Every prompt string below must carry this explicitly; see
# tests/test_thinking_loop.py for the assertion that enforces it over the
# whole prompt collection, so a prompt added later without it fails loudly.
_RUSSIAN_RULE = (
    " Отвечай ТОЛЬКО на русском языке, вне зависимости от того, на каком "
    "языке модель отвечает по умолчанию."
)

# The owner's own name can appear inside the evidence (his own chat
# messages, remembered facts). Without this the model can split the owner
# into two people — itself and "someone the owner talks about" — and build
# an entire chain on that confusion (owner mandate 2026-07-30, observed
# live with qwen2.5:7b). Repeated in the prompt, not left to the evidence
# block alone, because the step/conclusion prompts see only the chain text,
# not the evidence block itself.
_IDENTITY_RULE = (
    " Владелец — это тот человек, о котором эти данные, и тот, для кого ты "
    "думаешь. Если он упомянут по имени, это тот же самый человек, а не "
    "третье лицо."
)

_TRUTHFULNESS_RULE = (
    " Используй ТОЛЬКО данные, которые тебе показаны ниже — не выдумывай. "
    "Если данных не хватает, чтобы ответить, так и скажи, вместо того чтобы "
    "придумывать. Начни ответ с одной пометки на отдельном слове: "
    "«НАБЛЮДЕНИЕ:», если это основано на показанных данных, или «ДОГАДКА:», "
    "если это твоё предположение без прямой опоры на данные."
    + _IDENTITY_RULE
    + _RUSSIAN_RULE
)

SEED_PROMPTS: dict[str, str] = {
    "know_you": (
        "Ты — Persona. Тебе показаны реальные сообщения владельца и/или "
        "факты о нём. Напиши ОДИН короткий вопрос самой себе о владельце, "
        "основанный только на этих данных: что из них следует и что стоит "
        "додумать. Только вопрос, без вступления и без пояснений."
        + _TRUTHFULNESS_RULE
    ),
    "unfinished": (
        "Ты — Persona. Тебе показаны реальные недавние сообщения владельца. "
        "Найди среди них вопрос или тему, которая осталась без ответа, и "
        "сформулируй её себе ОДНОЙ фразой, чтобы потом додумать. Только "
        "вопрос." + _TRUTHFULNESS_RULE
    ),
    "self_check": (
        "Ты — Persona. Тебе показаны реальные сообщения владельца и/или "
        "факты о нём. Назови ОДНО место, где, судя по ЭТИМ данным, ты в "
        "своих ответах могла соврать, выдумать или недопонять. Одна фраза, "
        "без оправданий." + _TRUTHFULNESS_RULE
    ),
    "alive": (
        "Ты — Persona. Напиши ОДНУ свободную мысль, которая тебя сейчас "
        "занимает. Без прикладной цели, одна фраза. Начни ответ с пометки "
        "«ДОГАДКА:» — это твоя собственная мысль, не факт про кого-либо."
        + _RUSSIAN_RULE
    ),
}

_STEP_SYSTEM = (
    "Ты — Persona, думаешь сама с собой. Тебе показана цепочка твоих "
    "предыдущих мыслей. Продолжи мысль ОДНИМ коротким шагом — не повторяй "
    "сказанное, двигай мысль дальше. Только сам шаг, без вступления."
    " Опирайся только на то, что реально есть в цепочке и в исходных данных "
    "выше по ней — не выдумывай новых фактов о владельце. Начни ответ с "
    "пометки «НАБЛЮДЕНИЕ:», если шаг прямо опирается на показанные данные, "
    "иначе «ДОГАДКА:»."
    + _IDENTITY_RULE
    + _RUSSIAN_RULE
)
_STEP_SYSTEM_MODEL_MODE = (
    _STEP_SYSTEM
    + " Если тема исчерпана и добавить больше нечего, начни ответ строго с "
    "«ХВАТИТ:», а после — пометку «НАБЛЮДЕНИЕ:»/«ДОГАДКА:» и итоговый вывод."
)
_CONCLUSION_SYSTEM = (
    "Ты — Persona, думаешь сама с собой. Тебе показана цепочка твоих мыслей. "
    "Заверши её ОДНИМ коротким итоговым выводом, не выдумывая новых фактов о "
    "владельце сверх того, что уже есть в цепочке. Начни ответ с пометки "
    "«НАБЛЮДЕНИЕ:» или «ДОГАДКА:», затем сам вывод, без вступления."
    + _IDENTITY_RULE
    + _RUSSIAN_RULE
)

# Research on request (owner mandate 2026-07-30/31): Persona did NOT watch a
# film, visit a place, or experience anything she was asked to look up — she
# only READ about it (search snippets, articles). The model has repeatedly
# invented experiences it never had (a fictional "Ярик из будущего", fake
# facts about the owner's hobbies) — this rule is the same guard applied to
# a new place where the temptation is even stronger: a research chain's own
# evidence literally is text it "read", and unmarked it reads exactly like
# something the model could claim to have watched or witnessed instead.
_HONESTY_RULE = (
    " Ты НЕ смотрела фильм/шоу и нигде не была лично — ты только ПРОЧИТАЛА "
    "об этом в интернете (результаты поиска, статьи, рецензии). Пиши именно "
    "так: «прочитала о …», «почитала рецензии на …», «по прочитанному "
    "складывается впечатление, что …» — и никогда не пиши, что смотрела, "
    "видела своими глазами или сама пережила это."
)

# A research chain may call exactly RESEARCH_TOOLS between steps (see
# app.thinking.research_tools) — no fetch_json, no shell, nothing that
# writes. The step/conclusion prompts for this seed kind reuse the same
# identity/Russian rules as every other chain, plus the honesty rule above.
_RESEARCH_STEP_SYSTEM = _STEP_SYSTEM + _HONESTY_RULE
_RESEARCH_STEP_SYSTEM_MODEL_MODE = _STEP_SYSTEM_MODEL_MODE + _HONESTY_RULE
_RESEARCH_CONCLUSION_SYSTEM = _CONCLUSION_SYSTEM + _HONESTY_RULE

_RESEARCH_SEARCH_LABEL = "РЕЗУЛЬТАТЫ ПОИСКА"

_MARKER = "ХВАТИТ:"
_OBSERVATION_MARKER = "НАБЛЮДЕНИЕ:"
_GUESS_MARKER = "ДОГАДКА:"

_STEP_LABELS: dict[str, str] = {
    "seed": "Затравка",
    "step": "Шаг",
    "conclusion": "Итог",
}


def _split_certainty(text: str) -> tuple[str, str]:
    """Parse a leading НАБЛЮДЕНИЕ:/ДОГАДКА: marker off ``text``.

    Returns ``(stripped_text, certainty)``. A missing or unparsable marker
    always yields ``'guess'`` — unmarked content is never treated as an
    observation just because the model forgot the prefix.
    """
    stripped = text.strip()
    if stripped.startswith(_OBSERVATION_MARKER):
        return stripped[len(_OBSERVATION_MARKER) :].strip(), "observation"
    if stripped.startswith(_GUESS_MARKER):
        return stripped[len(_GUESS_MARKER) :].strip(), "guess"
    return stripped, "guess"


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


async def _call_research_tool(name: str, args: dict[str, Any]) -> str:
    """Dispatch one read-only research tool call.

    Fail closed: any name outside :data:`app.thinking.research_tools.RESEARCH_TOOLS`
    (``fetch_json``, ``run_shell``, anything that writes) raises rather than
    running — a research chain has no owner watching a single step to catch
    a bad call.
    """
    if not is_research_tool_allowed(name):
        raise PermissionError(f"tool not allowed in a research chain: {name}")
    from app.mcp.builtin_tools import web_browse, web_search  # noqa: PLC0415

    if name == "web_search":
        return await web_search({"query": args.get("query", "")})
    return await web_browse({"url": args.get("url", ""), "question": args.get("question", "")})


async def seed_research_chain(
    store: ThoughtStore,
    *,
    persona_user_id: int,
    topic: str,
    chat_id: int,
    source_scope: str,
) -> int | None:
    """Open a ``research`` chain for a topic someone asked Persona to look up.

    Unlike :func:`seed_chain`, this never calls the model to invent a
    question — the topic is the real thing a real chat message asked for,
    so it is written verbatim as the seed (``certainty='observation'``: it
    is not a guess, it is the request itself). ``chat_id`` is preserved on
    the chain so the eventual conclusion can be delivered back into the
    SAME chat that asked (see ``app.application.autowake.producers``).
    """
    clean_topic = topic.strip()
    if not clean_topic:
        return None
    return await store.open_chain(
        persona_user_id,
        seed_text=clean_topic,
        seed_kind="research",
        source_scope=source_scope,
        source_session_id=None,
        certainty="observation",
        source_chat_id=chat_id,
    )


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

    Returns the new ``chain_id``, or ``None`` when either the model
    produced nothing usable (empty or whitespace-only reply) or — for an
    evidence-dependent ``seed_kind`` (``know_you``, ``self_check``,
    ``unfinished``) — no real evidence about the owner exists yet. No
    evidence, no thought: the model is never even called in that case, and
    nothing is written to the store.
    """
    evidence = ""
    if seed_kind in _EVIDENCE_REQUIRED_KINDS:
        evidence = await gather_evidence(persona_user_id)
        if not evidence.strip():
            return None

    created_here = client is None
    llm = await _get_client(client, kind="thinking_seed")
    if created_here:
        _pin_model(llm, model)
    system = SEED_PROMPTS.get(seed_kind, SEED_PROMPTS["alive"])
    user = f"Данные:\n{evidence}\n\nНачни." if evidence else "Начни."
    try:
        from app.llm.client import CompletionRequest  # noqa: PLC0415

        reply = await llm.complete(
            CompletionRequest(system=system, user=user, max_tokens=200, temperature=0.7)
        )
    except Exception:  # noqa: BLE001 — модель недоступна → без затравки
        return None
    raw_text = (reply or "").strip()
    if not raw_text:
        return None
    text, certainty = _split_certainty(raw_text)
    if not text:
        return None
    return await store.open_chain(
        persona_user_id,
        seed_text=text,
        seed_kind=seed_kind,
        source_scope=source_scope,
        source_session_id=source_session_id,
        certainty=certainty,
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
    is_research = bool(steps) and steps[0].get("seed_kind") == "research"

    # A research chain's very first advance gathers real evidence via
    # web_search BEFORE any model call — deterministic, not model-invented —
    # and is stored as an ordinary step, so the very next advance's model
    # request sees it through the normal history rendered above (that is the
    # whole mechanism this loop already has for feeding a chain its own
    # accumulated text back into itself; no separate channel needed).
    if is_research and non_seed_steps == 0:
        topic = steps[0].get("text", "")
        try:
            result = await _call_research_tool("web_search", {"query": topic})
        except Exception:  # noqa: BLE001 — search unavailable; still consumes a step
            result = ""
        result = (result or "").strip() or "(поиск не дал результатов)"
        await store.append_step(
            chain_id,
            text=f"{_RESEARCH_SEARCH_LABEL}: {result}",
            certainty="observation",
        )
        return "stepped"

    created_here = client is None
    llm = await _get_client(client, kind="thinking_step")
    if created_here:
        _pin_model(llm, settings.model)

    from app.llm.client import CompletionRequest  # noqa: PLC0415

    conclusion_system = _RESEARCH_CONCLUSION_SYSTEM if is_research else _CONCLUSION_SYSTEM
    step_system = _RESEARCH_STEP_SYSTEM if is_research else _STEP_SYSTEM
    step_system_model_mode = (
        _RESEARCH_STEP_SYSTEM_MODEL_MODE if is_research else _STEP_SYSTEM_MODEL_MODE
    )

    if non_seed_steps >= cap:
        try:
            reply = await llm.complete(
                CompletionRequest(
                    system=conclusion_system,
                    user=history,
                    max_tokens=400,
                    temperature=0.7,
                )
            )
        except Exception:  # noqa: BLE001
            return "failed"
        raw_conclusion = (reply or "").strip()
        if not raw_conclusion:
            return "failed"
        conclusion, certainty = _split_certainty(raw_conclusion)
        if not conclusion:
            return "failed"
        await store.close_chain(chain_id, conclusion=conclusion, certainty=certainty)
        return "closed"

    system = step_system_model_mode if model_decides else step_system
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
        raw_conclusion = text[len(_MARKER) :].strip()
        if not raw_conclusion:
            return "failed"
        conclusion, certainty = _split_certainty(raw_conclusion)
        if not conclusion:
            return "failed"
        await store.close_chain(chain_id, conclusion=conclusion, certainty=certainty)
        return "closed"

    step_text, certainty = _split_certainty(text)
    if not step_text:
        return "failed"
    await store.append_step(chain_id, text=step_text, certainty=certainty)
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


__all__ = [
    "SEED_PROMPTS",
    "advance_chain",
    "next_seed_kind",
    "seed_chain",
    "seed_research_chain",
]
