# Full Tool Access Implementation Plan (slice E)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Persona gets real reach — the internet everywhere, the full toolset in the owner's own chat — while the one thing that can execute code stays behind an explicit owner confirmation and never fires on a stranger's text.

**Architecture:** Three separate changes. The heuristic that silently muted tools on short messages is deleted. Tool availability becomes a small explicit policy function keyed on who is speaking and where. Execution-class tools stop running inline and instead park a row in `telegram_pending_action`, which an inline button confirms — reading the arguments back from the database, never from the button payload.

**Tech Stack:** Python 3.12, aiosqlite, Telegram Bot API, pytest.

## Global Constraints

- Interpreter `./.venv/Scripts/python.exe`, from the repo root via Git Bash. Windows.
- Bump BOTH `app/__init__.py` `__version__` AND `CACHE_VERSION` in `app/web/static/sw.js` to the SAME value. Current: `2.30.24`. Task N moves to `2.30.(24+N)`.
- Next free migration number: verify — expect `226`.
- Never run the full suite (13 minutes). Targeted runs only.
- FOREGROUND only — no background tasks or monitors.
- Tolerated pre-existing failures: exactly 4 in `tests/test_ambient_group.py`.
- Stage ONLY files your task touches. NEVER stage `app/web/templates/dashboard.html`, `reminders.html`, `search.html`, `timeline.html`, `skills-lock.json`, or the untracked `app/web/routes/dashboard_ai.py`, `search_ai.py`, `timeline_ai.py`, `ai_calendar.py`.
- Do NOT push. The controller pushes.

## The one rule this plan exists to protect

Persona's Telegram input includes **text written by other people** — in the owner's group there are Дима, Олег, and two AI agents. Anything that executes code must therefore never be reachable from that text. This is not a limit on the owner: the owner keeps every capability, and the confirmation exists so that a message written by somebody else cannot become a command. The owner explicitly chose the confirmation button when this was designed.

Read-only internet is different in kind and is opened everywhere: fetching a page cannot alter the machine, and `_url_is_safe` in `app/mcp/builtin_tools.py` already blocks private-network and loopback targets.

---

### Task 1: Delete the heuristic, make the policy explicit

**Files:**
- Modify: `app/integrations/telegram/worker.py`
- Create: `app/integrations/telegram/tool_policy.py`
- Modify: `app/adapters/conversation/legacy.py`
- Test: `tests/test_telegram_tool_policy.py`

**Interfaces:**
- Produces, in `app/integrations/telegram/tool_policy.py`:
  - `READ_ONLY_TOOLS: frozenset[str]` — `web_search`, `web_browse`, `fetch_json`
  - `def allowed_tools(*, is_owner: bool, is_group: bool) -> frozenset[str] | None` — `None` means "every tool"; a set means exactly those; an empty set means none.

- [ ] **Step 1: Write the failing tests.** Cover, by calling `allowed_tools` directly:
  - owner in a private chat → `None` (everything, no heuristic, no message-length threshold);
  - owner in a group → exactly `READ_ONLY_TOOLS`;
  - non-owner in a group → exactly `READ_ONLY_TOOLS`;
  - non-owner in a private chat → empty set (they should not have reached the model at all, but the policy must not depend on that being true elsewhere);
  - no execution-class name (`run_shell`, `run_mac`, `install_mcp`, `install_skill`, `write_file`, `delete_path`) appears in any group result — assert this by name, so adding a dangerous tool later cannot silently widen group access.

- [ ] **Step 2: Run them, confirm they fail.**

- [ ] **Step 3: Implement `tool_policy.py`**, then rewire `worker.py`: delete `_owner_tools_needed` entirely and stop gating on it. The old line read `allow_tools=private_owner and _owner_tools_needed(clean_text)`, which muted tools whenever the owner's message was short and contained no keyword — "найди фото кота" is 15 characters and lost its tools. Replace with the policy call.

`legacy.py` currently branches on `command.allow_tools` alone (around lines 232, 357, 389). It must now also respect WHICH tools are allowed, so a group turn is offered only the read-only set. Read those three sites and thread the allowed set through rather than bolting on a second flag.

- [ ] **Step 4: Run tests plus `tests/test_telegram_integration.py`, confirm green.**

- [ ] **Step 5:** Bump to `2.30.25`, commit `"Give the owner every tool and open read-only internet everywhere"`.

---

### Task 2: Park execution-class tools behind confirmation

**Files:**
- Create: `app/storage/migrations/226_telegram_pending_action.sql`
- Create: `app/integrations/telegram/pending_actions.py`
- Test: `tests/test_telegram_pending_actions.py`

**Interfaces:**
- Produces, on `PendingActionStore`:
  - `park(persona_user_id: int, *, tool_name: str, args: dict[str, Any], chat_id: int) -> int` returns the pending id
  - `claim(persona_user_id: int, pending_id: int, *, now: datetime) -> dict[str, Any] | None` — returns the parked row and marks it consumed, atomically; returns `None` when unknown, expired, already consumed, or belonging to another tenant
  - `TTL_MINUTES: int = 15`

- [ ] **Step 1: Write the migration**

```sql
CREATE TABLE IF NOT EXISTS telegram_pending_action (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_user_id INTEGER NOT NULL,
    telegram_chat_id INTEGER NOT NULL,
    tool_name       TEXT NOT NULL,
    args_json       TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_telegram_pending_action_live
    ON telegram_pending_action(persona_user_id, consumed_at, expires_at);
```

- [ ] **Step 2: Write the failing tests.** Cover:
  - park then claim returns the exact args that were parked;
  - a second claim of the same id returns `None` — one press, one execution;
  - a claim after `expires_at` returns `None`;
  - a claim by a different `persona_user_id` returns `None`;
  - two concurrent claims of the same id yield exactly one non-`None` result. Use `asyncio.gather` for this; the single-execution property is the entire point of the table and must be proven, not assumed.

- [ ] **Step 3: Run them, confirm they fail.**

- [ ] **Step 4: Implement.** `claim` must do its check-and-mark in ONE `write_transaction` with a conditional `UPDATE ... WHERE id=? AND consumed_at IS NULL AND expires_at > datetime('now') AND persona_user_id=?`, and treat `rowcount == 0` as `None`. A read-then-write leaves a race that double-executes.

- [ ] **Step 5: Run tests, confirm green.**

- [ ] **Step 6:** Bump to `2.30.26`, commit `"Park execution-class Telegram actions behind a one-shot claim"`.

---

### Task 3: Wire the confirmation into Telegram

**Files:**
- Modify: `app/integrations/telegram/worker.py`
- Modify: `app/integrations/telegram/api.py`
- Test: `tests/test_telegram_confirm_flow.py`

**Interfaces:**
- Consumes: `PendingActionStore` (Task 2), `allowed_tools` (Task 1).

- [ ] **Step 1: Write the failing tests.** Cover:
  - an execution-class tool call from the owner's private chat parks a row and sends a card showing the tool name and the exact command, with two inline buttons, and does NOT execute;
  - pressing confirm executes once and marks consumed;
  - pressing confirm twice executes once;
  - a callback from a non-owner is refused;
  - a callback whose payload carries different arguments than the parked row executes the PARKED arguments — assert this explicitly, because reading args from `callback_data` would make the whole confirmation theatre;
  - an execution-class request originating in a GROUP is refused outright and parks nothing, regardless of who sent it.

- [ ] **Step 2: Run them, confirm they fail.**

- [ ] **Step 3: Implement.** `api.py` needs an inline-keyboard send and a callback-answer; read its existing `send_message` and `call` helpers and follow their shape. The callback payload carries ONLY the pending id. Arguments come from the database row.

- [ ] **Step 4: Run tests plus `tests/test_telegram_integration.py`, confirm green.**

- [ ] **Step 5:** Bump to `2.30.27`, commit `"Confirm Telegram execution actions with a one-shot button"`.

---

## Manual verification

1. Restart uvicorn via `ops/persona_watchdog.py`'s own `_kill_existing()` / `_start()` — the watchdog never reloads code on its own.
2. In the owner's private chat, send a short tool-needing message such as «найди фото кота» and confirm tools now engage (before this plan, a message that short silently lost them).
3. In the group, ask for a web search — it should work; ask to install something — it should refuse.
4. In private, ask to install an MCP server — a card with the exact command and two buttons should appear; confirm it, then press again and see it refuse the second time.

## Deliberately not in this plan

- Changing `run_mac`'s existing on-device blocklist. It already exists and is out of scope.
- Removing `_url_is_safe`. It blocks private-network targets, not the internet.
- Any path that lets group text reach an execution-class tool.
