# Persona Mac Agent

The Mac agent records compressed audio segments and periodic WebP screenshots
on a macOS workstation and uploads them to your Persona server. It runs in the
background as a LaunchAgent and is fully controllable from the command line.

> If you only want to *try* the agent without installing it system-wide, run
> `python -m persona_agent run --config ./dev.toml` from a checkout. The
> instructions below cover the production setup.

---

## Prerequisites

- **macOS 12 (Monterey) or newer.** Earlier versions lack the Screen Capture
  permission model the agent depends on.
- **Python 3.11+** on `PATH`. The easiest install is
  `brew install python@3.12`. The installer searches for
  `python3.13` / `python3.12` / `python3.11` / `python3` in that order.
- **Homebrew** is suggested but not required. If you don't use it, install
  Python from [python.org](https://www.python.org/downloads/macos/).
- A **pairing token** issued by your Persona server (`/admin/agents`).
- About 200 MB of free disk space for the virtualenv.

The agent does *not* require Xcode, Rosetta, sudo, or admin rights, with one
optional exception: linking `/usr/local/bin/persona-agent` for convenience. If
that directory isn't writable the installer falls back to the in-venv binary
and prints a warning.

---

## Quick start

1. **Pair the Mac.** On any browser, sign in to the Persona web UI and open
   `/admin/agents`. Click **Pair new agent**, give it a name (the Mac's
   hostname is a good default), and copy the one-time token. Tokens look like
   `PA-xxxxxxxxxxxxxxxx` and can only be viewed once.

2. **Clone the repo on the Mac** (or copy the `mac-agent/` folder onto it):

   ```bash
   git clone https://github.com/your-org/persona.git
   cd persona/mac-agent
   ```

3. **Run the installer.** Interactive mode will ask for the server URL and
   token:

   ```bash
   bash install/install.sh
   ```

   Or pass them as flags (handy for MDM rollouts):

   ```bash
   bash install/install.sh \
       --server https://persona.example.com \
       --token  PA-xxxxxxxxxxxxxxxx \
       --non-interactive
   ```

   The installer will:
   - create `~/.persona-agent/venv` and `pip install -e ..` into it,
   - write `~/.config/persona-agent.toml` (mode 0600),
   - render and copy `com.persona.agent.plist` into
     `~/Library/LaunchAgents/`,
   - `launchctl bootstrap gui/$(id -u) ...` the agent,
   - open **System Settings -> Privacy & Security -> Screen Recording**.

4. **Grant permissions.** When System Settings opens, enable
   `persona-agent` (you may see it as the Python binary the first time --
   re-toggle it after the first capture so the entry resolves to the symlink)
   under each of:

   - **Screen Recording** -- required for screen capture.
   - **Microphone** -- required for audio capture.
   - **Accessibility** -- optional; only needed if you turn on UI-event
     tagging in `persona-agent.toml`.

   After toggling, kick the agent so it re-reads permissions:

   ```bash
   launchctl kickstart -k gui/$(id -u)/com.persona.agent
   ```

5. **Verify.**

   ```bash
   persona-agent status
   tail -f ~/Library/Logs/persona-agent.log
   ```

   The status command should report `state: running`, the server URL, and the
   timestamp of the last successful upload. The server's `/admin/agents` page
   will show the Mac as **online** within a few seconds.

---

## Optional: keep the Mac awake

macOS sleeps when the lid closes, which kills the LaunchAgent. If this Mac is
meant to stream 24/7 (e.g. a docked workstation), either:

- enable **System Settings -> Battery / Energy -> "Prevent automatic sleeping
  on power adapter when the display is off"**, or
- run the bundled `caffeinate-helper.sh` alongside the agent:

  ```bash
  # one-off, foreground:
  bash install/caffeinate-helper.sh -- persona-agent run

  # tie to an existing process:
  nohup bash install/caffeinate-helper.sh \
      --pid "$(pgrep -f persona-agent)" >/dev/null 2>&1 &
  ```

  Add it to its own LaunchAgent if you want it to survive reboots. The
  helper is intentionally separate from the main plist so users on battery can
  opt out.

---

## What data leaves the Mac?

The agent uploads only two kinds of payloads to the server URL configured in
`~/.config/persona-agent.toml`:

| Source  | Format            | Cadence (default)            | Notes                                          |
| ------- | ----------------- | ---------------------------- | ---------------------------------------------- |
| Audio   | Opus in WebM      | 15-second segments           | Captured from the default input device.        |
| Screen  | WebP (lossy q=70) | One frame per active window  | Captured from the main display only.          |

All uploads are HTTPS POSTs to your server. The bearer token from
`persona-agent.toml` is sent in the `Authorization` header. Nothing is sent to
Anthropic, Apple, or third parties -- the agent talks to exactly one host.

Local caches live under `~/.persona-agent/cache/` and are purged after a
successful upload. Logs are in `~/Library/Logs/persona-agent.{log,err}`.

To disable a capture source, edit `~/.config/persona-agent.toml`:

```toml
[capture]
audio  = false   # mic stays untouched
screen = true
```

then `launchctl kickstart -k gui/$(id -u)/com.persona.agent`.

---

## Day-to-day usage

```bash
persona-agent status     # is it running? when did it last upload?
persona-agent pause      # stop uploading until `resume`
persona-agent resume
persona-agent ping       # one-shot connectivity check against the server
persona-agent logs -f    # tail both stdout + stderr logs together
```

Restart the LaunchAgent after editing the config:

```bash
launchctl kickstart -k gui/$(id -u)/com.persona.agent
```

Force a full reload (e.g. after a plist change):

```bash
launchctl bootout    gui/$(id -u)/com.persona.agent
launchctl bootstrap  gui/$(id -u) ~/Library/LaunchAgents/com.persona.agent.plist
```

---

## Troubleshooting

### `launchctl bootstrap` says `Bootstrap failed: 5: Input/output error`

Almost always a malformed plist. Validate it:

```bash
plutil -lint ~/Library/LaunchAgents/com.persona.agent.plist
```

If it's fine, check that the plist is owned by you and mode 644.

### `launchctl bootstrap` says `Service already loaded`

Bootout first, then bootstrap again:

```bash
launchctl bootout   gui/$(id -u)/com.persona.agent
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.persona.agent.plist
```

The installer does this automatically on re-runs.

### The agent crashes immediately with code `78` ("config error")

```bash
cat ~/Library/Logs/persona-agent.err
```

Common causes: missing `~/.config/persona-agent.toml`, wrong token, or the
file accidentally became world-readable (the agent refuses to start unless
the file is mode 0600). Fix with:

```bash
chmod 600 ~/.config/persona-agent.toml
```

### Microphone not detected

1. Make sure another app (Zoom, OBS, etc.) isn't holding exclusive access.
2. Confirm `persona-agent` is checked in **System Settings -> Privacy &
   Security -> Microphone**.
3. List devices the agent can see:

   ```bash
   persona-agent devices
   ```

   Pick the device name and set it in `~/.config/persona-agent.toml`:

   ```toml
   [capture]
   audio_device = "MacBook Pro Microphone"
   ```

   Then kickstart the agent.

### Screen capture stays black

macOS quietly denies screen capture if the app isn't checked in **Privacy &
Security -> Screen Recording**. After enabling it, you must restart the
LaunchAgent (`launchctl kickstart -k ...`); a toggle alone isn't enough.

If the binary shown in Screen Recording is `python3.12` instead of
`persona-agent`, that's normal -- macOS attributes permission to the
underlying executable. Leave it enabled.

### Network errors / 401 / 403

- `401 Unauthorized` -- the pairing token is wrong or has been revoked. Pair
  again at `/admin/agents` and rerun `install.sh`.
- `403 Forbidden` -- the server recognizes the token but the agent has been
  paused on the server side. Unpause it from `/admin/agents`.
- `connect: Operation timed out` -- the server URL or firewall is wrong; try
  `curl -v https://your-server/healthz` from this Mac.

### Where are the logs?

```text
~/Library/Logs/persona-agent.log     # stdout (info)
~/Library/Logs/persona-agent.err     # stderr (warnings, tracebacks)
```

`persona-agent logs -f` tails both with timestamps interleaved.

### How do I pause without uninstalling?

```bash
persona-agent pause            # soft pause; LaunchAgent stays loaded
launchctl unload ~/Library/LaunchAgents/com.persona.agent.plist  # hard stop
```

`persona-agent resume` undoes the soft pause; `launchctl load ...` (or a
reboot) undoes the hard stop.

---

## Uninstall

```bash
cd /path/to/persona/mac-agent
bash install/uninstall.sh           # interactive: asks before deleting data
bash install/uninstall.sh --purge   # nuke venv + config + logs
bash install/uninstall.sh --keep-config   # only remove the LaunchAgent
```

The uninstaller will also remind you to revoke the token at
`/admin/agents` on the server -- the script can't do that for you because the
token gives write access to the server.

---

## File layout

```
mac-agent/
  README.md                          (this file)
  install/
    install.sh                       installer described above
    uninstall.sh                     reverse of install.sh
    com.persona.agent.plist          LaunchAgent template with @@PLACEHOLDERS@@
    caffeinate-helper.sh             optional sleep-blocker wrapper
```

The `persona-agent` Python package itself lives one level up
(`mac-agent/../`) and is installed in editable mode by `install.sh`, so any
`git pull` followed by `launchctl kickstart -k gui/$(id -u)/com.persona.agent`
picks up new code without a reinstall.
