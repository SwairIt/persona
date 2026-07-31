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

import re
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
_RESEARCH_CONCLUSION_SYSTEM = _CONCLUSION_SYSTEM + _HONESTY_RULE

# Owner mandate 2026-07-31: in model-decides mode Persona must decide for
# HERSELF whether she needs to look further or already knows enough — not
# just deterministically run one search and reason over it forever. Only
# offered in cap_mode="model" (the mode where she already decides WHEN to
# stop with ХВАТИТ:) — the same marker, plus two more for the same choice.
# ОТКРЫВАЮ: is restricted in code (see _handle_research_open) to a URL that
# actually appeared in this chain's own search results — a model that can
# invent a URL to "open" is exactly the failure this project has been bitten
# by before (see _IDENTITY_RULE's history).
_RESEARCH_ACTIONS_RULE = (
    " У тебя есть ровно три допустимых варианта ответа на этом шаге, выбери "
    "один: начни ответ строго с «ИЩУ: <запрос>», если нужно поискать ещё "
    "что-то; начни строго с «ОТКРЫВАЮ: <ссылка>», если хочешь открыть одну "
    "из ссылок, которая уже встречалась в результатах поиска ЭТОЙ цепочки "
    "(ссылку, которой не было в результатах поиска, открыть нельзя — не "
    "выдумывай ссылки); или начни строго с «ХВАТИТ: <вывод>», если данных "
    "уже достаточно, чтобы ответить."
)
_RESEARCH_STEP_SYSTEM_MODEL_MODE = (
    _STEP_SYSTEM_MODEL_MODE + _HONESTY_RULE + _RESEARCH_ACTIONS_RULE
)

_RESEARCH_SEARCH_LABEL = "РЕЗУЛЬТАТЫ ПОИСКА"
_RESEARCH_PAGE_LABEL = "СТРАНИЦА"


def _research_search_attempted(steps: list[dict[str, Any]]) -> bool:
    """True once a web_search has been attempted (and its outcome
    recorded) anywhere in this chain's stored steps.

    Defect 4 (owner-observed live run, "лабиринт фавна"): a research chain
    ran 15 speculative steps with no search ever attempted, because the
    mandatory-first-search branch below used to be gated on
    ``non_seed_steps == 0`` — a proxy for "has this chain searched yet"
    that is only true the very first time a *freshly created* chain is
    advanced. A chain already mid-flight when that gate was introduced (or
    any future bug that appends a non-search step before the gate runs)
    would have ``non_seed_steps > 0`` forever after and would never search
    at all, no matter how many times it advances — the actual cause, not
    a missing extra guard. Checking the chain's own recorded steps for a
    search label instead of counting them makes the invariant a structural
    fact about the chain's history rather than an assumption about when it
    was created relative to this code.
    """
    return any(
        str(step.get("text", "")).startswith(_RESEARCH_SEARCH_LABEL) for step in steps
    )

# Bounded research loop (owner mandate 2026-07-31): a small model left to
# call web_search/web_browse without a hard limit will not stop on its own.
# At most this many of EACH per chain, counting the deterministic first
# search too — see _research_progress/_handle_research_search/_handle_research_open.
_MAX_LOOKUPS_PER_CHAIN = 5

_SEARCH_MARKER = "ИЩУ:"
_OPEN_MARKER = "ОТКРЫВАЮ:"

_URL_RE = re.compile(r"https?://\S+")
_BRACKET_RE = re.compile(r"«([^»]+)»")

# Substrings a web_search result carries when it found nothing usable: a
# clean "no hits" reply from any provider, an outright [error], or the
# local placeholder written above when the tool call raised. Checked with
# `in` (not a prefix match) because the stored text is always prefixed with
# the "РЕЗУЛЬТАТЫ ПОИСКА: " label.
_NO_RESULTS_MARKERS = ("[ok] ничего не найдено", "[error]", "(поиск не дал результатов)")


def _search_found_nothing(result: str) -> bool:
    """True when a web_search reply carries no usable evidence to reason from."""
    return any(marker in result for marker in _NO_RESULTS_MARKERS)

_MARKER = "ХВАТИТ:"
_OBSERVATION_MARKER = "НАБЛЮДЕНИЕ:"
_GUESS_MARKER = "ДОГАДКА:"

_STEP_LABELS: dict[str, str] = {
    "seed": "Затравка",
    "step": "Шаг",
    "conclusion": "Итог",
}

# Defect 1 (owner-observed, live run "лабиринт фавна", 2026-07): the model
# wrote "Хватит:" and even "Хвatiт:" — mixed Latin/Cyrillic — and the exact
# upper-case match on _MARKER never fired, so the chain never closed and
# repeated the same paragraph eight times. This table folds the common
# Latin lookalikes onto their Cyrillic counterparts (а/a, е/e, о/o, с/c,
# х/x, и/i, т/t) so marker matching is homoglyph-tolerant, on top of being
# case-insensitive and whitespace-tolerant. Length-preserving (one char in,
# one char out) so offsets into the original string still line up after
# translation — this is relied on by ``_match_leading_marker`` below.
_HOMOGLYPH_TABLE = str.maketrans(
    {
        "a": "а",
        "e": "е",
        "o": "о",
        "c": "с",
        "x": "х",
        "i": "и",
        "t": "т",
    }
)


def _canon(s: str) -> str:
    """Fold case and the Latin/Cyrillic homoglyph mix onto one canonical
    form, for tolerant marker matching (see ``_HOMOGLYPH_TABLE``)."""
    return s.lower().translate(_HOMOGLYPH_TABLE)


def _match_leading_marker(text: str, marker: str, *, allow_label: bool = False) -> str | None:
    """Return the text after ``marker`` if ``text`` starts with it, else
    ``None``. Tolerant of: case, surrounding whitespace, the Latin/Cyrillic
    homoglyph mix (``_canon``), and — when ``allow_label`` is set — one
    leading НАБЛЮДЕНИЕ:/ДОГАДКА: label before the marker (the model has been
    observed emitting the certainty label before ХВАТИТ: instead of after,
    e.g. ``"Наблюдение:\\n\\nХватит: всё понятно"``).

    Both ``text`` and ``marker`` are canonicalised through ``_canon``, which
    is length-preserving (one character maps to exactly one character), so
    an index found in the canonical form is a valid index into the
    original ``stripped`` text too.
    """
    stripped = text.strip()
    canon = _canon(stripped)
    offset = 0
    if allow_label:
        for label in (_OBSERVATION_MARKER, _GUESS_MARKER):
            label_canon = _canon(label)
            if canon.startswith(label_canon):
                rest = canon[len(label_canon) :]
                skipped_ws = len(rest) - len(rest.lstrip())
                offset = len(label_canon) + skipped_ws
                break
    marker_canon = _canon(marker)
    if canon[offset:].startswith(marker_canon):
        return stripped[offset + len(marker_canon) :]
    return None


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


# Defect 2 (same live run): steps 6-11 of the observed chain were
# near-identical restatements of the same paragraph — nothing noticed,
# because nothing in code was watching for it. The model cannot be trusted
# to police its own repetition (it demonstrably didn't), so this is a code
# guard: a new step whose normalised text is near-identical to either of
# the previous two steps closes the chain immediately instead of appending
# the duplicate.
_REPETITION_SIMILARITY_THRESHOLD = 0.85


def _normalize_for_repetition(text: str) -> str:
    """Case-fold and collapse whitespace for repetition comparison only —
    deliberately not the homoglyph canonicalisation above, which is for
    short exact markers, not paragraph similarity."""
    return " ".join(text.split()).casefold()


def _is_near_duplicate_step(candidate: str, previous: str) -> bool:
    """True when ``candidate`` is a near-restatement of ``previous``."""
    import difflib  # noqa: PLC0415

    a = _normalize_for_repetition(candidate)
    b = _normalize_for_repetition(previous)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= _REPETITION_SIMILARITY_THRESHOLD


def _repeats_recent_step(step_text: str, steps: list[dict[str, Any]]) -> bool:
    """True when ``step_text`` near-duplicates either of the chain's last
    two stored rows (seed, step, or conclusion — whichever they are)."""
    return any(
        _is_near_duplicate_step(step_text, str(row.get("text", "")))
        for row in steps[-2:]
    )


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


def _extract_bracket(text: str) -> str | None:
    """Pull the first «…» quoted value out of a stored step's text, or
    ``None`` when there isn't one."""
    match = _BRACKET_RE.search(text)
    return match.group(1) if match else None


def _clean_url(url: str) -> str:
    """Strip whitespace and common trailing punctuation off a URL taken
    from a model reply or from a stored search-result step's text."""
    return url.strip().rstrip(").,;»\"'")


def _research_progress(
    steps: list[dict[str, Any]],
) -> tuple[list[str], list[str], set[str]]:
    """Reconstruct a research chain's own history of lookups from its
    stored steps — no separate table, the chain's own text IS the state.

    Returns ``(queries_searched, urls_opened, urls_seen_in_search_results)``.
    The first two are in step order (for the ``_MAX_LOOKUPS_PER_CHAIN``
    count and dedup); the third is the set ``ОТКРЫВАЮ:`` is validated
    against — a URL never handed back by ``web_search`` in this chain may
    never be opened.
    """
    queries: list[str] = []
    opens: list[str] = []
    seen_urls: set[str] = set()
    for step in steps:
        text = str(step.get("text", ""))
        if text.startswith(_RESEARCH_SEARCH_LABEL):
            query = _extract_bracket(text)
            if query:
                queries.append(query)
            seen_urls.update(_clean_url(u) for u in _URL_RE.findall(text))
        elif text.startswith(_RESEARCH_PAGE_LABEL):
            url = _extract_bracket(text)
            if url:
                opens.append(url)
    return queries, opens, seen_urls


async def _handle_research_search(
    store: ThoughtStore, chain_id: int, steps: list[dict[str, Any]], query: str
) -> str:
    """Handle an ``ИЩУ: <query>`` reply: run the search, dedup, and enforce
    ``_MAX_LOOKUPS_PER_CHAIN`` — all in code, never left to the prompt."""
    query = query.strip()
    if not query:
        return "failed"
    queries, _opens, _seen = _research_progress(steps)
    if len(queries) >= _MAX_LOOKUPS_PER_CHAIN:
        await store.append_step(
            chain_id,
            text=(
                "НАБЛЮДЕНИЕ: лимит поисков в этой цепочке исчерпан "
                f"({_MAX_LOOKUPS_PER_CHAIN}) — больше искать нельзя, нужно "
                "закончить вывод тем, что уже есть."
            ),
            certainty="observation",
        )
        return "stepped"
    normalized = query.lower()
    if any(normalized == used.strip().lower() for used in queries):
        await store.append_step(
            chain_id,
            text=(
                f"НАБЛЮДЕНИЕ: запрос «{query}» уже искала в этой цепочке — "
                "результат не изменится, повторный поиск не нужен."
            ),
            certainty="observation",
        )
        return "stepped"
    try:
        result = await _call_research_tool("web_search", {"query": query})
    except Exception:  # noqa: BLE001 — search unavailable; still consumes a step
        result = ""
    result = (result or "").strip() or "(поиск не дал результатов)"
    await store.append_step(
        chain_id,
        text=f"{_RESEARCH_SEARCH_LABEL} «{query}»: {result}",
        certainty="observation",
    )
    return "stepped"


async def _handle_research_open(
    store: ThoughtStore, chain_id: int, steps: list[dict[str, Any]], url: str
) -> str:
    """Handle an ``ОТКРЫВАЮ: <url>`` reply: refuse anything not already
    seen in this chain's own search results, dedup, and enforce
    ``_MAX_LOOKUPS_PER_CHAIN`` — all in code, never left to the prompt."""
    clean = _clean_url(url)
    if not clean:
        return "failed"
    _queries, opens, seen_urls = _research_progress(steps)
    if len(opens) >= _MAX_LOOKUPS_PER_CHAIN:
        await store.append_step(
            chain_id,
            text=(
                "НАБЛЮДЕНИЕ: лимит открытых страниц в этой цепочке исчерпан "
                f"({_MAX_LOOKUPS_PER_CHAIN}) — больше открывать нельзя, нужно "
                "закончить вывод тем, что уже есть."
            ),
            certainty="observation",
        )
        return "stepped"
    if any(clean == opened.strip() for opened in opens):
        await store.append_step(
            chain_id,
            text=(
                f"НАБЛЮДЕНИЕ: ссылка «{clean}» уже открывалась в этой "
                "цепочке — повторно открывать не нужно."
            ),
            certainty="observation",
        )
        return "stepped"
    if clean not in seen_urls:
        await store.append_step(
            chain_id,
            text=(
                f"НАБЛЮДЕНИЕ: ссылка «{clean}» не встречалась в результатах "
                "поиска этой цепочки, поэтому открыть её нельзя — отказ."
            ),
            certainty="observation",
        )
        return "stepped"
    try:
        result = await _call_research_tool("web_browse", {"url": clean})
    except Exception:  # noqa: BLE001 — page unavailable; still consumes a step
        result = ""
    result = (result or "").strip() or "(страницу не удалось открыть)"
    await store.append_step(
        chain_id,
        text=f"{_RESEARCH_PAGE_LABEL} «{clean}»: {result}",
        certainty="observation",
    )
    return "stepped"


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
    #
    # Gated on whether a search was actually recorded (``_research_search_
    # attempted``), not on ``non_seed_steps == 0`` — see that function's
    # docstring for why the step-count proxy is the actual defect 4 cause.
    if is_research and not _research_search_attempted(steps):
        topic = steps[0].get("text", "")
        try:
            result = await _call_research_tool("web_search", {"query": topic})
        except Exception:  # noqa: BLE001 — search unavailable; still consumes a step
            result = ""
        result = (result or "").strip() or "(поиск не дал результатов)"
        await store.append_step(
            chain_id,
            text=f"{_RESEARCH_SEARCH_LABEL} «{topic}»: {result}",
            certainty="observation",
        )
        # No usable results: never feed an empty search into further model
        # reasoning steps — that emptiness is exactly what the model has
        # repeatedly filled with confident invention (a fake character, fake
        # facts about the owner, a world-famous film declared "a metaphor").
        # Close the chain right here, honestly, in code — not in a prompt
        # the model can ignore.
        if _search_found_nothing(result):
            conclusion = f"не нашла информации о «{topic}»"
            await store.close_chain(chain_id, conclusion=conclusion, certainty="observation")
            return "closed"
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

    if model_decides:
        stop_remainder = _match_leading_marker(text, _MARKER, allow_label=True)
        if stop_remainder is not None:
            raw_conclusion = stop_remainder.strip()
            if not raw_conclusion:
                return "failed"
            conclusion, certainty = _split_certainty(raw_conclusion)
            if not conclusion:
                return "failed"
            await store.close_chain(chain_id, conclusion=conclusion, certainty=certainty)
            return "closed"

    # She decides for herself whether she needs to look further (owner
    # mandate 2026-07-31) — only offered alongside ХВАТИТ:, in model-decides
    # research chains. Bounds (_MAX_LOOKUPS_PER_CHAIN, dedup, URL provenance)
    # are enforced inside the handlers, in code, not left to the prompt.
    if is_research and model_decides:
        search_remainder = _match_leading_marker(text, _SEARCH_MARKER)
        if search_remainder is not None:
            return await _handle_research_search(store, chain_id, steps, search_remainder)
        open_remainder = _match_leading_marker(text, _OPEN_MARKER)
        if open_remainder is not None:
            return await _handle_research_open(store, chain_id, steps, open_remainder)

    step_text, certainty = _split_certainty(text)
    if not step_text:
        return "failed"
    if _repeats_recent_step(step_text, steps):
        # Defect 2: nothing new to say — close now with this step's own
        # text as the conclusion rather than appending a duplicate and
        # letting the chain grind on repeating itself.
        await store.close_chain(chain_id, conclusion=step_text, certainty=certainty)
        return "closed"
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
