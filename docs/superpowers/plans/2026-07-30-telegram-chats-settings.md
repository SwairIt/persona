# Telegram Chats Settings Implementation Plan (slices C+D)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Move Telegram chat access out of in-chat commands and the "is it pinned?" accident, onto a settings page the owner controls — and switch on full reading plus history analysis for the owner's own group.

**Architecture:** A new `telegram_chat_pref` table holds one row per chat with the owner's chosen mode and ingest flag. The existing `telegram_allowed_chat_ids` kv stays the source of truth for "may reply", so the transport needs no reteaching; the page writes through to it. `pinned_ingest.sync_once` stops selecting dialogs by `dialog.pinned` and selects the owner's chosen set instead, falling back to pinned only when the owner has chosen nothing.

**Tech Stack:** Python 3.12, aiosqlite, FastAPI, Jinja2, Telethon (user account, read-only), pytest.

## Global Constraints

- Interpreter `./.venv/Scripts/python.exe`, run from the repo root via Git Bash. Windows.
- Bump BOTH `app/__init__.py` `__version__` AND `CACHE_VERSION` in `app/web/static/sw.js` to the SAME value. Current: `2.30.21`. Task N moves to `2.30.(21+N)`.
- New settings pages MUST be registered in `app/web/routes/settings_hub.py` → `_CATEGORIES` (CLAUDE.md rule) or they are unreachable.
- Next free migration number: verify — `224` was used by the thinking slice, so expect `225`.
- Adding routes trips `tests/test_architecture_gates.py::test_registered_route_count_stays_within_budget` (currently `1_100`). That gate is working as intended: review, ratchet, add a one-line rationale comment in the existing style.
- Never run the full suite (13 minutes). Targeted runs only.
- FOREGROUND only — no background tasks or monitors.
- Tolerated pre-existing failures: exactly 4 in `tests/test_ambient_group.py`.
- Stage ONLY files your task touches. NEVER stage `app/web/templates/dashboard.html`, `reminders.html`, `search.html`, `timeline.html`, `skills-lock.json`, or the untracked `app/web/routes/dashboard_ai.py`, `search_ai.py`, `timeline_ai.py`, `ai_calendar.py`.
- Do NOT push. The controller pushes.
- **Read-only means read-only:** the Telethon path must never send, edit, react or mark-as-read. It logs in as the owner's own account; anything else would act as the owner without the owner.
- **Group content stays group-scoped:** ingesting a group must not make its content owner-private. `SourceScope.GROUP` and `SAFE_SOURCE_SCOPES` in `app/domains/autowake/policy.py` already encode this — never bypass them.

---

### Task 1: Per-chat preferences

**Files:**
- Create: `app/storage/migrations/225_telegram_chat_pref.sql`
- Modify: `app/integrations/telegram/repository.py`
- Test: `tests/test_telegram_chat_pref.py`

**Interfaces:**
- Produces, on `TelegramRepository`:
  - `set_chat_pref(chat_id: int, *, mode: str, ingest: bool, title: str = "") -> None` where `mode` is `"reply"` | `"read"` | `"ignore"`
  - `chat_pref(chat_id: int) -> dict[str, Any] | None`
  - `list_chat_prefs() -> list[dict[str, Any]]`
  - `ingest_chat_ids() -> set[int]`

- [ ] **Step 1: Write the migration**

```sql
CREATE TABLE IF NOT EXISTS telegram_chat_pref (
    telegram_chat_id INTEGER PRIMARY KEY,
    title            TEXT NOT NULL DEFAULT '',
    mode             TEXT NOT NULL DEFAULT 'read'
                         CHECK (mode IN ('reply', 'read', 'ignore')),
    ingest           INTEGER NOT NULL DEFAULT 0 CHECK (ingest IN (0, 1)),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
```

- [ ] **Step 2: Write failing tests** covering: a pref round-trips; `mode="reply"` ALSO adds the chat to the existing `telegram_allowed_chat_ids` kv and any other mode removes it (this write-through is what keeps the transport working unchanged); `ingest_chat_ids()` returns only chats with `ingest=1`; an unknown chat returns `None` rather than raising.

- [ ] **Step 3: Run tests, confirm they fail.**

- [ ] **Step 4: Implement.** Read `app/integrations/telegram/repository.py` first — `_ALLOWED_CHATS_KEY`, `allowed_chat_ids()` and the surrounding style are already there. The write-through to the kv MUST happen in the same `write_transaction` as the pref write, or a crash between them leaves the page and the transport disagreeing about who Persona answers.

- [ ] **Step 5: Run tests, confirm they pass.**

- [ ] **Step 6:** Bump to `2.30.22`, commit `"Store per-chat Telegram preferences"`.

---

### Task 2: Owner-selected ingest instead of "is it pinned"

**Files:**
- Modify: `app/integrations/telegram/pinned_ingest.py`
- Test: `tests/test_telegram_pinned_ingest.py`

**Interfaces:**
- Consumes: `ingest_chat_ids()` from Task 1.

- [ ] **Step 1: Write the failing test.** With prefs selecting one chat, `sync_once` imports THAT dialog and ignores an unrelated pinned one. With no prefs stored at all, behaviour falls back to today's pinned-only selection so nothing breaks for an owner who never opens the page. Use a fake Telethon client exposing `get_dialogs`, as the existing tests in that file already do — read them first.

- [ ] **Step 2: Run it, confirm it fails.**

- [ ] **Step 3: Implement.** In `sync_once` (around line 105) replace the `dialog.pinned` filter with: load `ingest_chat_ids()`; if non-empty select dialogs whose `int(dialog.id)` is in it; if empty keep the current pinned filter. Keep `_replace_pinned_set` semantics — the table name stays, only the selection rule changes. Do not rename the table or the worker; that is churn with no benefit and would break the registry entry.

- [ ] **Step 4: Run tests, confirm they pass.**

- [ ] **Step 5:** Bump to `2.30.23`, commit `"Select ingest chats by owner choice, not by pinned status"`.

---

### Task 3: The chats settings page

**Files:**
- Create: `app/web/routes/telegram_chats.py`
- Create: `app/web/templates/telegram_chats.html`
- Modify: `app/web/routes/settings_hub.py`, `app/web/main.py`, `tests/test_architecture_gates.py`
- Test: `tests/test_telegram_chats_web.py`

**Interfaces:**
- Produces: `GET /settings/telegram-chats`, `POST /settings/telegram-chats/{chat_id}`.

- [ ] **Step 1: Write failing tests** covering: the page lists a chat Persona has seen; POST sets mode and ingest and 303-redirects; a non-owner gets 403; an unknown chat id is rejected rather than silently creating a pref for a chat that does not exist.

- [ ] **Step 2: Run them, confirm they fail.**

- [ ] **Step 3: Implement the route.** Owner gate: reuse the `_owner_id(session)` pattern from `app/web/routes/telegram_people.py`. Checkboxes: unchecked boxes are ABSENT from the body, so declare `str = Form("")` and compare `== "on"`; `bool` 422s.

The chat list is the union of: distinct `telegram_chat_id` in `telegram_person_message`, the ids in `telegram_allowed_chat_ids`, and rows already in `telegram_chat_pref`. Show each chat's title where known, its id, its message count, and its current mode and ingest flag.

- [ ] **Step 4: Implement the template**, extending `base.html` and following the Tailwind conventions in `app/web/templates/telegram_person.html`. Light and dark theme both.

- [ ] **Step 5: Register** the router in `app/web/main.py` and both `_CATEGORIES` entries in `settings_hub.py`:

```python
    "/settings/telegram-chats": "telegram телеграм чаты группы доступ анализ история ингест",
```
```python
            ("/settings/telegram-chats", "💬 Чаты Telegram — где отвечать, что читать и разбирать"),
```

- [ ] **Step 6: Run tests**, ratchet the route budget with a rationale comment, confirm green.

- [ ] **Step 7:** Bump to `2.30.24`, commit `"Put Telegram chat access on the site"`.

---

### Task 4: Switch on the owner's group

**Files:** none — this is a data change plus verification.

- [ ] **Step 1:** Set the pref for chat `-5026288199` (the owner's «персик, клодик, индик» group) to `mode="reply"`, `ingest=True`, via the repository API, against the live database at `C:\Users\Yaroslav\.persona\persona.db`.

- [ ] **Step 2:** Verify `allowed_chat_ids()` still contains it and `ingest_chat_ids()` now contains it.

- [ ] **Step 3:** Report — do NOT attempt to run the Telethon import yourself. It authenticates as the owner's Telegram account; the session file `~/.persona/telegram-owner.session` exists, but the worker runs it on its own schedule. State in the report what the owner should see and when.

---

## Deliberately not in this plan

- Slice E (full tool access) — its own plan.
- Renaming `telegram_pinned_chat` / the pinned worker. The selection rule changed; the storage did not.
- Any write path through Telethon. Read-only is a safety property, not an oversight.
