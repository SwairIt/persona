# One-tap capture hotkey

Persona ships a tiny `persona-cli capture` subcommand that takes one
screenshot, runs it through the same dedup + thumbnail pipeline the
background worker uses, and prints the new `screenshot_id`. No web UI
required.

The `scripts\capture_now.bat` wrapper picks up `uv` (preferred) or falls
back to `python -m app capture --quiet`, so you can bind it to anything
that launches a `.bat` file. Below are three common setups on Windows.

## Option 1 — Windows shortcut + global hotkey (zero deps)

1. Right-click `scripts\capture_now.bat` -> *Send to* -> *Desktop (create shortcut)*.
2. Open the new shortcut's *Properties*.
3. In the *Shortcut key* field press your combo (e.g. `Ctrl + Alt + S`).
4. Set *Run* to *Minimized* so no console flashes up.
5. Click *OK*. Pressing the combo from any window now takes a snapshot.

Caveat: Windows-shortcut hotkeys only fire when the shortcut lives on
the desktop or in the Start menu.

## Option 2 — AutoHotkey v2

Install [AutoHotkey v2](https://www.autohotkey.com/) and create a
`persona.ahk` file with:

```ahk
#Requires AutoHotkey v2.0

; Ctrl + Alt + S -> snapshot now
^!s::Run('"C:\www-Yaroslav\Persona\scripts\capture_now.bat"', , "Hide")
```

Double-click the `.ahk` file (or drop it into your Startup folder) and
the hotkey works system-wide, including over full-screen apps.

## Option 3 — PowerToys Run

If you already use [PowerToys](https://learn.microsoft.com/windows/powertoys/)
the *Run* launcher (`Alt + Space`) can call the batch directly:

1. Open *Alt + Space*.
2. Type `> C:\www-Yaroslav\Persona\scripts\capture_now.bat` and press Enter.

Or pin it as a custom action via the *Keyboard Manager* module to map a
spare key (e.g. `F13`) to the same `.bat`.

## Verifying it worked

`capture_now.bat` runs with `--quiet`, so the only output is the new
screenshot's integer id. From a shell you can verify:

```powershell
uv run persona-cli capture --quiet
# -> 1234

uv run persona-cli search "whatever was on screen"
```

If the active-window detection misfires (rare, usually on locked or
UAC-elevated windows), use `--app NAME` to label the snapshot manually:

```powershell
uv run persona-cli capture --app "Notes" --quiet
```

The capture is fully offline — same dedup + thumbnail behaviour as the
background loop, just triggered on demand.
