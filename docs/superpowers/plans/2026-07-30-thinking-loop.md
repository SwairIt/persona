# Thinking Loop Implementation Plan (slices A+B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persona seeds herself a question after a conversation, develops it across steps with her own previous thoughts fed back in as input, and must end with a conclusion — all of it switchable and tunable from the site.

**Architecture:** A new `persona_thought` table stores chains of thoughts. A pure settings module reads owner choices from `kv_settings`. An application module seeds, advances and closes chains. A worker runs the loop only while the owner is idle and abandons the current step the moment the owner speaks. Sinks reuse what already exists: conclusions go to a diary page, worthwhile ones to memory via `reflection.py`'s promotion, and occasionally to chat via the existing rate-limited `PersonaImpulseProducer` — no second delivery path is built.

**Tech Stack:** Python 3.12, aiosqlite, FastAPI, Jinja2, pytest + pytest-asyncio, local Ollama via `app.llm.client`.

## Global Constraints

- Interpreter: `./.venv/Scripts/python.exe`, run from the repo root via Git Bash. Windows.
- Every feature commit bumps BOTH `app/__init__.py` `__version__` AND `CACHE_VERSION` in `app/web/static/sw.js` to the SAME value. Current: `2.30.13`. Task N moves to `2.30.(13+N)`.
- Any NEW settings page MUST be registered in `app/web/routes/settings_hub.py` → `_CATEGORIES`, or it is unreachable from the UI. This is a project rule from CLAUDE.md.
- Next free migration number is `222`. Migrations live in `app/storage/migrations/` and run in numeric order.
- Never run the full test suite — it takes 13 minutes. Targeted runs only.
- Foreground only: no background tasks, monitors, or `run_in_background`. A subagent deadlocked on that earlier in this project.
- Tolerated pre-existing failures anywhere: exactly 4 in `tests/test_ambient_group.py` (`assert 6 == 32` and three `test_decision_adapter_is_bounded_and_rejects_raw_metadata` cases). Anything else failing is yours.
- Stage ONLY files your task touches. The working tree carries unrelated modifications (`app/web/templates/dashboard.html`, `reminders.html`, `search.html`, `timeline.html`, `app/web/static/settings_palette.js`, `skills-lock.json`) and untracked files (`app/web/routes/*_ai.py`, `.claude/`, `.agents/`, `roadmap-cs.html`). NEVER stage those.
- Do NOT push. The controller pushes.
- **Owner priority is an invariant, not an optimisation:** the loop must never delay an owner turn. When in doubt, abandon the thought.
- **Privacy is fail-closed:** a chain inherits the `SourceScope` of whatever seeded it. Group-scoped chains never reach the owner's private diary as private, and never leave via autowake. `SAFE_SOURCE_SCOPES` in `app/domains/autowake/policy.py` already encodes this — reuse it, never bypass it.

**Scope:** slices A and B of `docs/superpowers/specs/2026-07-30-thinking-loop-and-settings-design.md`. Slices C (Telegram chats page), D (enable chat -5026288199) and E (full tool access) get their own plans after this one ships.

---

### Task 1: The thought store

**Files:**
- Create: `app/storage/migrations/222_persona_thought.sql`
- Create: `app/thinking/__init__.py`
- Create: `app/thinking/store.py`
- Test: `tests/test_thinking_store.py`

**Interfaces:**
- Consumes: `write_transaction`, `get_connection` from `app.storage.db`.
- Produces, all on `ThoughtStore`:
  - `open_chain(persona_user_id: int, *, seed_text: str, seed_kind: str, source_scope: str, source_session_id: int | None) -> int` returns `chain_id`
  - `append_step(chain_id: int, *, text: str) -> int` returns `step_no`
  - `close_chain(chain_id: int, *, conclusion: str) -> None`
  - `oldest_open_chain(persona_user_id: int) -> dict[str, Any] | None`
  - `chain_steps(chain_id: int) -> list[dict[str, Any]]` ordered by `step_no`
  - `steps_used_today(persona_user_id: int) -> int`

- [ ] **Step 1: Write the migration**

Create `app/storage/migrations/222_persona_thought.sql`:

```sql
CREATE TABLE IF NOT EXISTS persona_thought (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_user_id   INTEGER NOT NULL,
    chain_id          INTEGER NOT NULL,
    step_no           INTEGER NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('seed', 'step', 'conclusion')),
    seed_kind         TEXT NOT NULL CHECK (
                          seed_kind IN ('know_you', 'unfinished', 'self_check', 'alive')
                      ),
    text              TEXT NOT NULL,
    source_scope      TEXT NOT NULL,
    source_session_id INTEGER,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (chain_id, step_no)
);

CREATE TABLE IF NOT EXISTS persona_thought_chain (
    chain_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_user_id   INTEGER NOT NULL,
    seed_kind         TEXT NOT NULL,
    source_scope      TEXT NOT NULL,
    source_session_id INTEGER,
    status            TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open', 'closed')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_persona_thought_chain_open
    ON persona_thought_chain(persona_user_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_persona_thought_recent
    ON persona_thought(persona_user_id, created_at DESC);
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_thinking_store.py`:

```python
from __future__ import annotations

from app.thinking.store import ThoughtStore


async def _user(db, user_id: int = 7) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (user_id, f"{user_id}@example.test", "x"),
    )
    await db.commit()


async def test_chain_records_seed_steps_and_conclusion_in_order(db) -> None:
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7,
        seed_text="Что я поняла про владельца сегодня?",
        seed_kind="know_you",
        source_scope="owner_private",
        source_session_id=11,
    )
    assert await store.append_step(chain_id, text="Он не любит длинные ответы.") == 1
    assert await store.append_step(chain_id, text="Значит короче формулировать.") == 2
    await store.close_chain(chain_id, conclusion="Отвечать короче по умолчанию.")

    steps = await store.chain_steps(chain_id)
    assert [s["kind"] for s in steps] == ["seed", "step", "step", "conclusion"]
    assert [s["step_no"] for s in steps] == [0, 1, 2, 3]
    assert steps[-1]["text"] == "Отвечать короче по умолчанию."


async def test_closed_chain_is_not_returned_as_open(db) -> None:
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    assert (await store.oldest_open_chain(7))["chain_id"] == chain_id
    await store.close_chain(chain_id, conclusion="done")
    assert await store.oldest_open_chain(7) is None


async def test_a_half_finished_chain_stays_resumable(db) -> None:
    """Owner interrupted the loop: the chain must survive with its steps and be
    picked up again next time the owner goes quiet. This is the whole
    preemption mechanism — an interrupted step simply never gets written, and
    the chain is still the oldest open one."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="unfinished",
        source_scope="owner_private", source_session_id=None,
    )
    await store.append_step(chain_id, text="половина мысли")
    assert (await store.oldest_open_chain(7))["chain_id"] == chain_id
    assert len(await store.chain_steps(chain_id)) == 2


async def test_steps_used_today_counts_only_this_tenant(db) -> None:
    await _user(db, 7)
    await _user(db, 8)
    store = ThoughtStore()
    a = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    await store.append_step(a, text="one")
    b = await store.open_chain(
        8, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    await store.append_step(b, text="one")
    await store.append_step(b, text="two")
    assert await store.steps_used_today(7) == 2   # seed + 1 step
    assert await store.steps_used_today(8) == 3   # seed + 2 steps
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_thinking_store.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.thinking'`.

- [ ] **Step 4: Implement the store**

Create `app/thinking/__init__.py` containing only `from app.thinking.store import ThoughtStore` and `__all__ = ["ThoughtStore"]`.

Create `app/thinking/store.py` implementing `ThoughtStore` with the seven methods from **Interfaces** above, using `write_transaction()` for writes and `get_connection()` for reads, following the style of `app/integrations/telegram/people.py` (which is the closest existing repository in this codebase — read it first for conventions).

Rules the implementation must honour:
- `open_chain` inserts one `persona_thought_chain` row plus the `seed` row at `step_no = 0`, in ONE transaction.
- `append_step` allocates `step_no = MAX(step_no) + 1` for that chain inside the same transaction as the insert, so two concurrent appends cannot collide on the `UNIQUE (chain_id, step_no)` constraint.
- `close_chain` writes the `conclusion` row at the next `step_no` and flips the chain's `status` to `'closed'` with `closed_at`, in ONE transaction.
- `abandon_open_steps` does NOT delete anything and does NOT close the chain — the owner interrupted, and the chain must resume later with its steps intact. Implement it as a no-op over the rows plus whatever bookkeeping you need; if you find it needs no work at all given the schema, keep the method (callers depend on it) and say so in your report.
- `steps_used_today` counts `persona_thought` rows for that tenant with `date(created_at) = date('now')`.
- Text is clipped to 4000 characters on write.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_thinking_store.py -v`

Expected: PASS, 4 tests.

- [ ] **Step 6: Bump version and commit**

`__version__` → `2.30.14`, `CACHE_VERSION` → `'persona-v2.30.14'`.

```bash
git add app/storage/migrations/222_persona_thought.sql app/thinking/ tests/test_thinking_store.py app/__init__.py app/web/static/sw.js
git commit -m "Add the thought chain store"
```

---

### Task 2: Thinking settings

**Files:**
- Create: `app/thinking/settings.py`
- Test: `tests/test_thinking_settings.py`

**Interfaces:**
- Consumes: `get_kv`, `set_kv` from `app.storage.repository`; `get_connection` from `app.storage.db`.
- Produces:
  - `@dataclass(frozen=True, slots=True) ThinkingSettings` with fields
    `enabled: bool`, `cap_mode: str` (`"fixed"` | `"model"`), `step_cap: int`,
    `emergency_cap: int`, `daily_budget: int`, `seed_kinds: tuple[str, ...]`,
    `may_write_to_chat: bool`
  - `async def load_thinking_settings() -> ThinkingSettings`
  - `async def save_thinking_settings(settings: ThinkingSettings) -> None`
  - `DEFAULTS: ThinkingSettings` module constant
  - `ALL_SEED_KINDS: tuple[str, ...] = ("know_you", "unfinished", "self_check", "alive")`
  - `def effective_cap(settings: ThinkingSettings) -> int` — the number of steps after which a conclusion is forced: `step_cap` in `"fixed"` mode, `emergency_cap` in `"model"` mode.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking_settings.py`:

```python
from __future__ import annotations

import dataclasses

import pytest

from app.thinking.settings import (
    ALL_SEED_KINDS,
    DEFAULTS,
    ThinkingSettings,
    effective_cap,
    load_thinking_settings,
    save_thinking_settings,
)


async def test_defaults_are_returned_when_nothing_is_stored(db) -> None:
    loaded = await load_thinking_settings()
    assert loaded == DEFAULTS
    assert loaded.enabled is False, "thinking must be OFF until the owner turns it on"
    assert set(loaded.seed_kinds) == set(ALL_SEED_KINDS)


async def test_settings_round_trip(db) -> None:
    saved = ThinkingSettings(
        enabled=True,
        cap_mode="model",
        step_cap=7,
        emergency_cap=100,
        daily_budget=200,
        seed_kinds=("know_you", "alive"),
        may_write_to_chat=True,
    )
    await save_thinking_settings(saved)
    assert await load_thinking_settings() == saved


async def test_effective_cap_follows_the_mode() -> None:
    fixed = ThinkingSettings(
        enabled=True, cap_mode="fixed", step_cap=5, emergency_cap=50,
        daily_budget=60, seed_kinds=ALL_SEED_KINDS, may_write_to_chat=False,
    )
    assert effective_cap(fixed) == 5
    model = ThinkingSettings(
        enabled=True, cap_mode="model", step_cap=5, emergency_cap=50,
        daily_budget=60, seed_kinds=ALL_SEED_KINDS, may_write_to_chat=False,
    )
    assert effective_cap(model) == 50, (
        "in model-decides mode only the emergency cap forces a conclusion"
    )


async def test_corrupt_stored_values_fall_back_to_defaults(db) -> None:
    """A hand-edited or half-written kv row must not crash the worker."""
    from app.storage.db import get_connection
    from app.storage.repository import set_kv

    async with get_connection() as conn:
        await set_kv(conn, "thinking_step_cap", "не число")
        await set_kv(conn, "thinking_cap_mode", "нечто")
        await set_kv(conn, "thinking_seed_kinds", "know_you,выдумка")
        await conn.commit()

    loaded = await load_thinking_settings()
    assert loaded.step_cap == DEFAULTS.step_cap
    assert loaded.cap_mode == DEFAULTS.cap_mode
    assert loaded.seed_kinds == ("know_you",), "unknown seed kinds are dropped"


@pytest.mark.parametrize(
    "field", ["step_cap", "emergency_cap", "daily_budget"]
)
async def test_non_positive_numbers_are_rejected_on_save(db, field) -> None:
    # dataclasses.replace, not DEFAULTS.__dict__: the dataclass uses slots=True
    # and therefore has no __dict__.
    bad = dataclasses.replace(DEFAULTS, **{field: 0})
    with pytest.raises(ValueError):
        await save_thinking_settings(bad)


async def test_unknown_cap_mode_is_rejected_on_save(db) -> None:
    with pytest.raises(ValueError):
        await save_thinking_settings(dataclasses.replace(DEFAULTS, cap_mode="нечто"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_thinking_settings.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.thinking.settings'`.

- [ ] **Step 3: Implement the settings module**

Create `app/thinking/settings.py`. kv keys, all prefixed `thinking_`: `thinking_enabled`, `thinking_cap_mode`, `thinking_step_cap`, `thinking_emergency_cap`, `thinking_daily_budget`, `thinking_seed_kinds` (comma-separated), `thinking_may_write_to_chat`.

Defaults, matching the spec's table:

```python
DEFAULTS = ThinkingSettings(
    enabled=False,
    cap_mode="fixed",
    step_cap=5,
    emergency_cap=50,
    daily_budget=60,
    seed_kinds=ALL_SEED_KINDS,
    may_write_to_chat=False,
)
```

Loading must be total: any unparsable or out-of-range stored value falls back to that field's default rather than raising, because this runs inside a worker loop where a crash means the feature silently dies. Unknown seed kinds are dropped; if every stored seed kind is unknown, fall back to `ALL_SEED_KINDS`.

`save_thinking_settings` validates and raises `ValueError` on non-positive `step_cap`, `emergency_cap` or `daily_budget`, and on a `cap_mode` outside `{"fixed", "model"}`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_thinking_settings.py -v`

Expected: PASS.

- [ ] **Step 5: Bump version and commit**

`__version__` → `2.30.15`, `CACHE_VERSION` → `'persona-v2.30.15'`.

```bash
git add app/thinking/settings.py tests/test_thinking_settings.py app/__init__.py app/web/static/sw.js
git commit -m "Add thinking settings with total, fail-safe loading"
```

---

### Task 3: Seeding and advancing a chain

**Files:**
- Create: `app/thinking/loop.py`
- Test: `tests/test_thinking_loop.py`

**Interfaces:**
- Consumes: `ThoughtStore` (Task 1); `ThinkingSettings`, `effective_cap` (Task 2); `CompletionRequest`, `make_client` from `app.llm.client`.
- Produces:
  - `SEED_PROMPTS: dict[str, str]` — one system prompt per seed kind
  - `async def seed_chain(store, *, persona_user_id, seed_kind, source_scope, source_session_id, client=None) -> int | None` — asks the model for one thing to think about; returns the new `chain_id`, or `None` when the model produced nothing usable
  - `async def advance_chain(store, settings, *, chain_id, client=None) -> str` — returns one of `"stepped"`, `"closed"`, `"failed"`
  - `def next_seed_kind(settings: ThinkingSettings, previous: str | None) -> str` — round-robins through the enabled kinds

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking_loop.py`. Use a fake client rather than a live model:

```python
from __future__ import annotations

from typing import Any

from app.thinking.loop import advance_chain, next_seed_kind, seed_chain
from app.thinking.settings import ALL_SEED_KINDS, ThinkingSettings
from app.thinking.store import ThoughtStore


class FakeClient:
    """Returns queued replies and records the requests it was given."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> str:
        self.requests.append(request)
        return self.replies.pop(0) if self.replies else ""


async def _user(db, user_id: int = 7) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (user_id, f"{user_id}@example.test", "x"),
    )
    await db.commit()


def _settings(**over: Any) -> ThinkingSettings:
    base = dict(
        enabled=True, cap_mode="fixed", step_cap=2, emergency_cap=50,
        daily_budget=60, seed_kinds=ALL_SEED_KINDS, may_write_to_chat=False,
    )
    base.update(over)
    return ThinkingSettings(**base)


async def test_seed_opens_a_chain_with_the_model_text(db) -> None:
    await _user(db)
    store = ThoughtStore()
    client = FakeClient(["Почему он просит писать короче?"])
    chain_id = await seed_chain(
        store, persona_user_id=7, seed_kind="know_you",
        source_scope="owner_private", source_session_id=11, client=client,
    )
    assert chain_id is not None
    steps = await store.chain_steps(chain_id)
    assert steps[0]["kind"] == "seed"
    assert steps[0]["text"] == "Почему он просит писать короче?"


async def test_empty_model_reply_seeds_nothing(db) -> None:
    await _user(db)
    store = ThoughtStore()
    assert await seed_chain(
        store, persona_user_id=7, seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
        client=FakeClient(["   "]),
    ) is None


async def test_previous_steps_are_fed_back_as_input(db) -> None:
    """The whole point: the chain's own text must reach the model."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="СИД-МАРКЕР", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    client = FakeClient(["следующая мысль"])
    assert await advance_chain(store, _settings(), chain_id=chain_id, client=client) == "stepped"
    sent = client.requests[0]
    assert "СИД-МАРКЕР" in sent.user


async def test_chain_is_forced_closed_at_the_cap(db) -> None:
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    settings = _settings(step_cap=2)
    client = FakeClient(["шаг один", "шаг два", "итог"])
    assert await advance_chain(store, settings, chain_id=chain_id, client=client) == "stepped"
    assert await advance_chain(store, settings, chain_id=chain_id, client=client) == "stepped"
    assert await advance_chain(store, settings, chain_id=chain_id, client=client) == "closed"
    steps = await store.chain_steps(chain_id)
    assert steps[-1]["kind"] == "conclusion"
    assert await store.oldest_open_chain(7) is None


async def test_model_decides_mode_closes_when_the_model_says_so(db) -> None:
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    settings = _settings(cap_mode="model", step_cap=2, emergency_cap=50)
    client = FakeClient(["ХВАТИТ: всё понятно, вывод такой"])
    assert await advance_chain(store, settings, chain_id=chain_id, client=client) == "closed"


async def test_model_decides_mode_still_stops_at_the_emergency_cap(db) -> None:
    """The owner asked for model-decided depth; the emergency cap only stops a
    chain that never terminates."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    settings = _settings(cap_mode="model", step_cap=2, emergency_cap=3)
    client = FakeClient(["ещё", "ещё", "ещё", "ещё", "ещё"])
    outcomes = [
        await advance_chain(store, settings, chain_id=chain_id, client=client)
        for _ in range(4)
    ]
    assert outcomes[-1] == "closed"


def test_seed_kind_rotates_only_through_enabled_kinds() -> None:
    settings = ThinkingSettings(
        enabled=True, cap_mode="fixed", step_cap=5, emergency_cap=50,
        daily_budget=60, seed_kinds=("know_you", "alive"), may_write_to_chat=False,
    )
    assert next_seed_kind(settings, None) == "know_you"
    assert next_seed_kind(settings, "know_you") == "alive"
    assert next_seed_kind(settings, "alive") == "know_you"
    assert next_seed_kind(settings, "self_check") == "know_you"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_thinking_loop.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.thinking.loop'`.

- [ ] **Step 3: Implement the loop module**

Create `app/thinking/loop.py`.

`SEED_PROMPTS` — one Russian system prompt per kind, each asking for exactly one short thing to think about, no preamble:

```python
SEED_PROMPTS = {
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
```

`seed_chain` calls the model with the kind's prompt, strips the reply, returns `None` when it is empty after stripping, otherwise calls `store.open_chain(...)` and returns the id.

`advance_chain`:
1. Loads `store.chain_steps(chain_id)`.
2. Builds the user message from the chain so far — the seed and every step, in order, labelled — and this is the mechanism the whole feature exists for, so it must be the actual stored text, not a summary.
3. Counts existing non-seed steps. If that count is already `>= effective_cap(settings)`, ask the model for a closing conclusion, call `store.close_chain(...)`, return `"closed"`.
4. Otherwise ask for the next step. In `cap_mode == "model"` the system prompt additionally tells the model to begin its reply with `ХВАТИТ:` when the topic is exhausted; when the reply starts with that marker, strip the marker, `close_chain` with the remainder, and return `"closed"`.
5. An empty or failed reply returns `"failed"` without writing anything.
6. `max_tokens=400`, `temperature=0.7`.

`next_seed_kind` rotates through `settings.seed_kinds` in the order given by `ALL_SEED_KINDS`, returning the first enabled kind when `previous` is `None` or not in the enabled set.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_thinking_loop.py -v`

Expected: PASS, 7 tests.

- [ ] **Step 5: Bump version and commit**

`__version__` → `2.30.16`, `CACHE_VERSION` → `'persona-v2.30.16'`.

```bash
git add app/thinking/loop.py tests/test_thinking_loop.py app/__init__.py app/web/static/sw.js
git commit -m "Feed a thought chain back into the model and force a conclusion"
```

---

### Task 4: The worker — idle gate, preemption, daily budget

**Files:**
- Create: `app/workers/thinking_worker.py`
- Modify: `app/bootstrap/worker_registry.py`
- Test: `tests/test_thinking_worker.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3; `_is_quiet` from `app.chat.reflection`; `beat` from `app.workers.heartbeat`.
- Produces:
  - `async def run_thinking_worker() -> None` — the registry entry point
  - `async def tick(store, settings, *, persona_user_id, now, client=None) -> str` — one decision, returning `"disabled"`, `"busy"`, `"budget"`, `"stepped"`, `"closed"`, `"seeded"` or `"idle"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking_worker.py`. Test `tick` directly — never the infinite loop:

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.thinking.settings import ALL_SEED_KINDS, ThinkingSettings
from app.thinking.store import ThoughtStore
from app.workers.thinking_worker import tick


class FakeClient:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    async def complete(self, request: Any) -> str:
        return self.replies.pop(0) if self.replies else ""


async def _user(db, user_id: int = 7) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (user_id, f"{user_id}@example.test", "x"),
    )
    await db.commit()


def _settings(**over: Any) -> ThinkingSettings:
    base = dict(
        enabled=True, cap_mode="fixed", step_cap=5, emergency_cap=50,
        daily_budget=60, seed_kinds=ALL_SEED_KINDS, may_write_to_chat=False,
    )
    base.update(over)
    return ThinkingSettings(**base)


async def _quiet(_now: Any) -> bool:
    return True


async def _busy(_now: Any) -> bool:
    return False


async def test_disabled_setting_stops_everything(db, monkeypatch) -> None:
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _quiet)
    result = await tick(
        ThoughtStore(), _settings(enabled=False),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient([]),
    )
    assert result == "disabled"


async def test_owner_activity_blocks_thinking(db, monkeypatch) -> None:
    """Owner priority is an invariant: a busy model must never be used to think."""
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _busy)
    result = await tick(
        ThoughtStore(), _settings(),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["мысль"]),
    )
    assert result == "busy"


async def test_daily_budget_stops_the_loop(db, monkeypatch) -> None:
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _quiet)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    await store.append_step(chain_id, text="one")
    result = await tick(
        store, _settings(daily_budget=2),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["ещё"]),
    )
    assert result == "budget"


async def test_quiet_and_no_open_chain_seeds_one(db, monkeypatch) -> None:
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _quiet)
    store = ThoughtStore()
    result = await tick(
        store, _settings(),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["новая мысль"]),
    )
    assert result == "seeded"
    assert (await store.oldest_open_chain(7)) is not None


async def test_quiet_with_an_open_chain_advances_it(db, monkeypatch) -> None:
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _quiet)
    store = ThoughtStore()
    await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    result = await tick(
        store, _settings(),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["шаг"]),
    )
    assert result == "stepped"
```

Note: `_is_quiet` is monkeypatched with the module-level `_quiet` / `_busy` async helpers defined at the top of the file — `monkeypatch.setattr` replaces the name inside `app.workers.thinking_worker`, so the worker must import it as `from app.chat.reflection import _is_quiet` (a module-level name), not call it through the module.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_thinking_worker.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.workers.thinking_worker'`.

- [ ] **Step 3: Implement the worker**

Create `app/workers/thinking_worker.py`.

`tick` order of checks — this order IS the design, do not reorder:
1. `settings.enabled` false → `"disabled"`.
2. `await _is_quiet(now)` false → `"busy"`. The owner is active; never think.
3. `await store.steps_used_today(persona_user_id) >= settings.daily_budget` → `"budget"`.
4. `oldest_open_chain` exists → `advance_chain(...)`, return its outcome (`"stepped"` / `"closed"` / `"failed"`).
5. No open chain → pick `next_seed_kind`, `seed_chain(...)`; `"seeded"` on success, `"idle"` when the model returned nothing.

`run_thinking_worker` is the loop: read settings fresh every iteration (so toggling on the site takes effect without a restart), call `tick`, `beat` the heartbeat, and sleep — 60 seconds after a productive tick, 300 seconds after `"disabled"`, `"busy"`, `"budget"` or `"idle"`. Wrap each iteration in `try/except` so one failure never kills the worker.

Read `app/workers/dream_worker.py` first and follow its structure — it is the closest existing worker (idle-gated, LLM-driven, same cadence shape).

Seed provenance: use `SourceScope.OWNER_PRIVATE` for now, since the seed comes from the owner's own recent conversation. Group-seeded chains arrive in a later slice; the field exists so that change is data, not schema.

- [ ] **Step 4: Register the worker**

In `app/bootstrap/worker_registry.py`, add a `_spec` entry next to `dream-worker`:

```python
    _spec(
        "thinking-worker",
        "app.workers.thinking_worker",
        "run_thinking_worker",
        pass_controller=False,
        profiles=_FULL_AND_LEAN,
        cadence="self-directed thought chains while the owner is idle",
    ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_thinking_worker.py tests/test_worker_registry.py tests/test_architecture_gates.py -v`

Expected: PASS. If the registered-route or worker-count budget gate trips, review the addition and ratchet the budget in the test with a one-line rationale comment, following the existing comments there.

- [ ] **Step 6: Bump version and commit**

`__version__` → `2.30.17`, `CACHE_VERSION` → `'persona-v2.30.17'`.

```bash
git add app/workers/thinking_worker.py app/bootstrap/worker_registry.py tests/test_thinking_worker.py app/__init__.py app/web/static/sw.js
git commit -m "Run the thinking loop only while the owner is idle"
```

---

### Task 5: The settings page and the diary

**Files:**
- Create: `app/web/routes/thinking.py`
- Create: `app/web/templates/thinking_settings.html`
- Create: `app/web/templates/thoughts.html`
- Modify: `app/web/routes/settings_hub.py`
- Modify: `app/web/main.py` (router registration)
- Test: `tests/test_thinking_web.py`

**Interfaces:**
- Consumes: `load_thinking_settings`, `save_thinking_settings`, `ThinkingSettings`, `ALL_SEED_KINDS` (Task 2); `ThoughtStore` (Task 1).
- Produces: `GET /settings/thinking`, `POST /settings/thinking`, `GET /thoughts`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking_web.py`, following the auth and client conventions already used in `tests/test_web_routes.py` — read that file first and reuse its fixtures rather than inventing new ones. Cover:
- `GET /settings/thinking` renders and pre-selects the stored values.
- `POST /settings/thinking` with valid form data persists and 303-redirects.
- `POST` with `step_cap=0` is rejected with 400 and does NOT change the stored settings.
- `POST` with no seed-kind checkboxes ticked is rejected with 400 — a thinking loop with nothing to think about is a misconfiguration, not a silent no-op.
- `GET /thoughts` lists a chain that exists in the store.
- Both pages require the owner: a non-owner gets 403.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_thinking_web.py -v`

Expected: FAIL — routes do not exist.

- [ ] **Step 3: Implement the routes**

Create `app/web/routes/thinking.py` following the exact shape of `app/web/routes/theme.py` (GET renders, POST validates against a whitelist then `set_kv` then 303) and the owner gate of `app/web/routes/telegram_people.py` (`_owner_id(session)` raising 403).

The `POST` handler takes `enabled`, `cap_mode`, `step_cap`, `emergency_cap`, `daily_budget`, `may_write_to_chat` and repeated `seed_kinds` checkbox values as `Form(...)`, builds a `ThinkingSettings`, and lets `save_thinking_settings`'s `ValueError` become a 400.

Checkbox convention in this codebase: an unchecked box is absent from the form body, so declare them as `str = Form("")` and compare `== "on"`. Do NOT use `bool` — a missing field 422s.

`GET /thoughts` renders chains newest-first with their steps, and shows each chain's status and seed kind.

- [ ] **Step 4: Implement the templates**

Create both templates extending `base.html`, following the markup and Tailwind class conventions already in `app/web/templates/telegram_person.html`. The settings page must also show the live state the spec asks for: steps used today, whether a chain is open, and the current `may_write_to_chat` state. Both must work in light and dark theme — reuse existing classes rather than inventing colours.

- [ ] **Step 5: Register the routes and the hub entries**

Register the router in `app/web/main.py` alongside the other settings routers.

In `app/web/routes/settings_hub.py` add to `_CATEGORIES` — without this the pages are unreachable from the UI, which is a project rule:

```python
    "/settings/thinking": "мышление думать мысли цепочки размышления автономность дневник",
```

and the visible entry:

```python
            ("/settings/thinking", "🧠 Мышление — сама думает, потолок шагов, дневник мыслей"),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_thinking_web.py tests/test_architecture_gates.py -v`

Expected: PASS. Three new routes are added, so the registered-route budget gate will trip — review and ratchet `REGISTERED_ROUTE_BUDGET` with a one-line rationale comment in the existing style.

- [ ] **Step 7: Bump version and commit**

`__version__` → `2.30.18`, `CACHE_VERSION` → `'persona-v2.30.18'`.

```bash
git add app/web/routes/thinking.py app/web/templates/thinking_settings.html app/web/templates/thoughts.html app/web/routes/settings_hub.py app/web/main.py tests/test_thinking_web.py app/__init__.py app/web/static/sw.js
git commit -m "Put thinking settings and the thought diary on the site"
```

---

## Manual verification after the plan

1. Restart uvicorn — the watchdog (`ops/persona_watchdog.py`) only restarts a DEAD server, it never reloads code. Use its own functions: load it via `importlib.util.spec_from_file_location`, then `_kill_existing()`, `sleep 3`, `_start()`, and poll `_alive()`.
2. Open `/settings/thinking`, turn thinking on, leave the cap at 5.
3. Leave Persona alone for the quiet window (60 minutes by `_QUIET_MINUTES`) — or temporarily lower it to observe sooner.
4. Open `/thoughts` and confirm a chain appeared, developed, and closed with a conclusion.
5. Send a message mid-chain and confirm the chain stays open rather than being lost.

## Deliberately not in this plan

- Promotion of conclusions into long-term memory, and the occasional escape into chat via `PersonaImpulseProducer`. Both reuse existing machinery and belong in the next plan, once chains demonstrably produce sane conclusions on this hardware.
- Group-scoped seeds. The `source_scope` column exists so adding them is data, not schema.
- Slices C, D and E of the spec.
