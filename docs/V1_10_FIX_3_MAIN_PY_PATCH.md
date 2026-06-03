# v1.10 fix 3/3 — `app/web/main.py` lifespan patch (coordinator)

## Context

Migration `091_pause_on_boot.sql` (shipped this chunk) seeds
`kv_settings('capture_paused_on_boot', '0')`. The default flips the
boot-time behaviour: capture starts running immediately. A power-user
checkbox in the settings page lets people opt back in to the old
"paused on boot" behaviour.

The lifespan reader is the third leg of the fix. **The coordinator
owns `app/web/main.py` edits**, so this chunk did not touch that file
— but the exact patch needed is below, ready to drop in.

## Imports

Add the kv reader at the top of `app/web/main.py` next to the existing
`from app.storage.db import init_database` line:

```python
from app.storage.db import init_database, get_connection
from app.storage.repository import get_kv
```

(`get_connection` is already imported elsewhere in the file's
neighbourhood — check before duplicating.)

## Lifespan body — replace

Current (lines ~588-591):

```python
    controller.pause()
    log.info("persona.started", host=get_settings().host, port=get_settings().port)
```

Replace with:

```python
    # v1.10 fix 3/3 — capture runs on boot by default. The yellow
    # ``Пауза`` pill on first frame was confusing new users into
    # thinking Persona was broken. Power users who actually want the
    # old "paused on boot" behaviour can flip the
    # ``capture_paused_on_boot`` kv row to ``"1"`` via the settings
    # page (migration 091_pause_on_boot.sql seeds it as ``"0"``).
    #
    # The read happens after worker tasks are spawned so the pause
    # takes effect on the very first iteration of the capture loop
    # instead of racing it.
    async with get_connection() as conn:
        boot_pause_raw = await get_kv(conn, "capture_paused_on_boot")
    if (boot_pause_raw or "").strip() == "1":
        controller.pause()
        log.info("persona.boot_pause", paused=True, source="kv_setting")
    else:
        log.info("persona.boot_pause", paused=False, source="kv_setting")
    log.info("persona.started", host=get_settings().host, port=get_settings().port)
```

## Why a string compare and not a bool helper

Every other kv toggle in this codebase (`compact_mode`,
`grayscale_mode`, `reduce_motion`, `077_anim_toggle`) uses the same
``(raw or "").strip() == "1"`` shape. We deliberately mirror it here
so a future reader can find the pattern by grep and know exactly what
to expect: anything other than the literal ``"1"`` collapses to "off",
including ``None`` from a kv row that somehow went missing.

## Logger name

The settings POST handler logs to `persona.boot_pause`. The lifespan
reader should log to the same logger so a single
`grep persona.boot_pause` shows both the user's toggle action and
the actual boot-time decision.

## Verification checklist

After applying the patch:

1. Delete the kv row (`DELETE FROM kv_settings WHERE key='capture_paused_on_boot'`)
   then restart Persona — migration 091 re-seeds it as `"0"`, capture
   should run on boot (no yellow Пауза pill in the header).
2. Toggle the checkbox on `/settings` → row becomes `"1"` → restart →
   header shows Пауза, capture loop sits idle until you click Resume.
3. Toggle it back → row becomes `"0"` → restart → capture runs again.
4. Tail logs: every boot emits exactly one `persona.boot_pause`
   structured event with `paused=true|false` and `source="kv_setting"`.

## Files this chunk touched

- `app/storage/migrations/091_pause_on_boot.sql` — new migration
- `app/web/routes/settings.py` — kv reader + POST handler
- `app/web/templates/settings.html` — new "Capture behaviour"
  section with the checkbox
- `docs/V1_10_FIX_3_MAIN_PY_PATCH.md` — this note

## Files the coordinator must touch

- `app/web/main.py` — patch above
