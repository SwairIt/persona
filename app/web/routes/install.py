"""T18 (2026-06-07) — One-click installers for capture agents.

The /welcome page asks the user to run a Mac agent for screen capture.
Before T18 they had to: clone the repo, create venv, install deps,
write config.json with a hand-copied admin token, then figure out
launchd for auto-start. Most users gave up at step 1.

This module generates a **personalised installer** the user can run in
one terminal command. The installer script:
  * Clones the repo into ~/persona (or pulls if it's already there)
  * Creates the .venv, installs requirements
  * Writes config.json with the server URL + agent token baked in
  * Sets up a launchd plist so the agent starts on login

Endpoints:
  GET  /welcome/install/mac          — HTML page showing the one-liner
  POST /welcome/install/mac/mint     — mint a fresh agent_token (auth)
  GET  /api/install/mac.sh?t=TOKEN   — serve the installer script

The script is PUBLIC (no auth) because the user runs it from their Mac
terminal where they don't have the session cookie. The single-use ``t``
query token authorises one fetch; after that the script is downloaded
and the agent token inside it is the long-lived credential.
"""

from __future__ import annotations

import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.devices import list_devices, register_device, rotate_token
from app.logging_setup import get_logger
from app.remote_agents import create_agent
from app.web.templates_engine import templates

router = APIRouter(tags=["install"])
log = get_logger("persona.install")

# In-memory single-use install tokens. Wiped on restart — that's fine
# because they live <10 minutes anyway. Maps install_id → (agent_token,
# device_token, server_url, created_at_epoch).
#   * agent_token  — bearer for /api/agent/* ingest (screenshots/audio)
#   * device_token — X-Device-Token for /api/sync/* + /api/devices/heartbeat
#                    + /api/workspace/sync (T28 code-write-target sync)
_PENDING: dict[str, tuple[str, str, str, float]] = {}
_TTL_SECONDS = 600  # 10 minutes — plenty of time to copy-paste


def _purge_expired() -> None:
    now = time.time()
    expired = [k for k, (_, _, _, t) in _PENDING.items() if now - t > _TTL_SECONDS]
    for k in expired:
        _PENDING.pop(k, None)


async def _ensure_mac_device(user_id: int) -> str:
    """Return a fresh ``device_token`` for the user's Mac, provisioning the
    sync identity the one-click install previously skipped.

    Without this the installed agent only had the ingest ``token`` and the
    T28 sync loops (heartbeat + workspace pull) self-disabled — the Mac
    never showed on /devices and could not be a code-write-target. We
    reuse the user's existing ``mac`` device row (rotating its token) so
    the code-target selection survives a reinstall; otherwise we create
    one.
    """
    devices = await list_devices(user_id)
    mac = next((d for d in devices if d["kind"] == "mac"), None)
    if mac is not None:
        rotated = await rotate_token(user_id, mac["id"])
        if rotated is not None:
            return rotated["device_token"]
    created = await register_device(user_id, name="Mac", kind="mac")
    return created["device_token"]


def _detect_public_url(request: Request) -> tuple[str, bool]:
    """Best-guess of the externally-reachable URL.

    Returns ``(url, is_local_only)``. ``is_local_only`` is True when
    we couldn't find anything better than ``localhost`` / ``127.0.0.1``
    so the UI can warn that the install command won't reach the Mac.

    Order:
      1. X-Forwarded-Host header (set by devtunnels / reverse proxies)
         + X-Forwarded-Proto for scheme
      2. request.url (whatever the user opened in browser)
      3. Mark as local-only if netloc starts with localhost/127.0.0.1
    """
    fwd_host = request.headers.get("x-forwarded-host", "").strip()
    fwd_proto = request.headers.get("x-forwarded-proto", "").strip()
    if fwd_host:
        scheme = fwd_proto or request.url.scheme
        return f"{scheme}://{fwd_host}", False
    netloc = request.url.netloc
    scheme = request.url.scheme
    is_local = netloc.startswith("localhost") or netloc.startswith("127.0.0.1")
    return f"{scheme}://{netloc}", is_local


@router.get("/welcome/install/mac", response_class=HTMLResponse, response_model=None)
async def install_mac_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    install_id: str = Query(default=""),
) -> HTMLResponse:
    """Show the one-liner. If ``install_id`` is set + still valid, render
    the curl command with that token. Otherwise show the 'Generate' button."""
    _purge_expired()
    server_url, is_local = _detect_public_url(request)
    ready_command = ""
    if install_id and install_id in _PENDING:
        # zsh treats ``?`` as a glob char so the unquoted URL fails with
        # 'no matches found'. Single-quote the URL so both bash and zsh
        # see it as a literal.
        ready_command = (
            f"curl -fsSL '{server_url}/api/install/mac.sh?t={install_id}' | bash"
        )
    return templates.TemplateResponse(
        request,
        "install_mac.html",
        {
            "title": "Установка на Mac",
            "active_nav": "",
            "server_url": server_url,
            "is_local_only": is_local,
            "install_id": install_id,
            "ready_command": ready_command,
        },
    )


@router.post("/welcome/install/mac/mint", response_model=None)
async def install_mac_mint(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    """Mint a fresh agent token + stash it under a single-use install_id.

    The user is then redirected back to the install page with the
    install_id in the URL, which renders the ready-to-copy one-liner.
    """
    _purge_expired()
    user_id = session.get("user_id") if isinstance(session, dict) else None
    name = f"Mac (user {user_id})" if user_id else "Mac"
    try:
        _agent_id, raw_token = await create_agent(name, platform="mac")
    except Exception as exc:
        log.exception("install.mac.mint_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="failed to create agent") from exc

    # T29 — also provision the sync identity (device_token) so the installed
    # agent's heartbeat + workspace-sync loops actually run and the Mac
    # becomes a real /devices entry that can be picked as code-write-target.
    device_token = ""
    if user_id:
        try:
            device_token = await _ensure_mac_device(int(user_id))
        except Exception as exc:
            # Non-fatal: ingest still works without it; sync just stays off.
            log.warning("install.mac.device_provision_failed", error=str(exc))

    install_id = secrets.token_urlsafe(24)
    # Use the public URL so the token-bearing curl URL inside the script
    # itself also reaches the Mac. Falling back to the request URL would
    # bake ``localhost`` into config.json — the Mac agent would then try
    # to POST to its own loopback and silently 404.
    server_url, _is_local = _detect_public_url(request)
    _PENDING[install_id] = (raw_token, device_token, server_url, time.time())

    log.info(
        "install.mac.minted",
        install_id_prefix=install_id[:6],
        user_id=user_id,
        device_provisioned=bool(device_token),
    )
    return RedirectResponse(
        url=f"/welcome/install/mac?install_id={install_id}",
        status_code=303,
    )


@router.get("/api/install/mac.sh", response_class=PlainTextResponse, response_model=None)
async def install_mac_script(t: str = Query(...)) -> PlainTextResponse:
    """Serve the installer script. Single-use: the install_id is consumed
    on first GET so the URL stops working immediately afterwards.

    No auth dependency — the install_id IS the auth. It was minted
    server-side by an authenticated request via /welcome/install/mac/mint.
    """
    _purge_expired()
    record = _PENDING.pop(t, None)
    if record is None:
        # Generic 404-equivalent that won't be cached. We don't say
        # "expired" specifically because that helps attackers map TTLs.
        return PlainTextResponse(
            "# Install link expired. Refresh /welcome/install/mac to mint a new one.\n",
            status_code=410,
        )
    agent_token, device_token, server_url, _ = record

    # Quote everything safely. Tokens are url-safe base64 so they're
    # already shell-safe, but treat as user input anyway.
    safe_token = agent_token.replace("'", "'\\''")
    safe_device_token = device_token.replace("'", "'\\''")
    safe_url = server_url.replace("'", "'\\''")

    script = f"""#!/usr/bin/env bash
# Persona Mac agent installer — auto-generated on the server.
# Run from any terminal:  curl -fsSL {server_url}/api/install/mac.sh?t=... | bash
#
# This script:
#   1. Clones the Persona repo to ~/persona  (or pulls if it exists)
#   2. Creates a Python venv + installs requirements
#   3. Writes ~/.config/persona-agent.toml with server URL + tokens
#   4. Registers a launchd plist so the agent starts at login
#   5. Starts the agent immediately
#
# Re-running this command updates an existing install in place (git pull +
# fresh config + agent restart) — it is both the installer and the updater.

set -euo pipefail

REPO_DIR="$HOME/persona"
SERVER_URL='{safe_url}'
AGENT_TOKEN='{safe_token}'
DEVICE_TOKEN='{safe_device_token}'

echo "→ Persona Mac installer"
echo "  Server:  $SERVER_URL"
echo "  Repo:    $REPO_DIR"
echo ""

# 1. Repo
if [ -d "$REPO_DIR/.git" ]; then
    echo "→ Repo exists, pulling latest..."
    cd "$REPO_DIR" && git pull --ff-only
else
    echo "→ Cloning repo..."
    git clone https://github.com/SwairIt/persona.git "$REPO_DIR"
    cd "$REPO_DIR"
fi

# 2. venv + dependencies
cd "$REPO_DIR/mac-agent"
if [ ! -d ".venv" ]; then
    echo "→ Creating venv..."
    python3 -m venv .venv
fi
echo "→ Installing dependencies (может занять пару минут)..."
# Generous network timeouts/retries — pypi.org can be slow or flaky on some
# networks (the default 15s read-timeout was making installs fail with
# ReadTimeoutError). Set PERSONA_PIP_INDEX to a faster mirror if needed,
# e.g. PERSONA_PIP_INDEX=https://mirror.yandex.ru/mirrors/pypi/simple/
PIP_OPTS="--quiet --timeout 120 --retries 5"
if [ -n "${{PERSONA_PIP_INDEX:-}}" ]; then
    PIP_OPTS="$PIP_OPTS --index-url $PERSONA_PIP_INDEX"
    echo "  (использую зеркало: $PERSONA_PIP_INDEX)"
fi
# Upgrading pip is nice-to-have, not required — never abort on it.
./.venv/bin/pip install $PIP_OPTS --upgrade pip || true
# Core deps — required for the agent to even start, capture the screen and
# heartbeat to the server. (The deps live in pyproject.toml; there is no
# requirements.txt, so we install them explicitly here.) If these fail the
# agent can't run at all, so abort.
./.venv/bin/pip install $PIP_OPTS \
    "httpx>=0.28" "mss>=10.0" "pillow>=11.0" "imagehash>=4.3" "numpy>=1.26" \
    "click>=8.1" "structlog>=24.4" "pydantic>=2.10" "pydantic-settings>=2.7"
# Audio (voice transcription) is OPT-IN — its deps are heavy
# (openai-whisper pulls torch ~2GB; sounddevice needs PortAudio) and on a
# slow link the download dominates the install. Off by default: you get a
# fast, clean install with screen capture + sync. Enable voice by running
# the command with PERSONA_AGENT_VOICE=1 in front of it.
AUDIO_ENABLED=false
if [ "${{PERSONA_AGENT_VOICE:-0}}" = "1" ]; then
    # LITE voice stack — webrtcvad (~tiny C lib), NO torch. Records speech;
    # transcription happens on the SERVER. Few MB, not 2 GB.
    echo "→ Installing voice deps (lite: webrtcvad, без torch)..."
    if ./.venv/bin/pip install $PIP_OPTS \
            "sounddevice>=0.5" "webrtcvad>=2.0" "scipy>=1.13"; then
        AUDIO_ENABLED=true
    else
        echo "⚠ Голос не установился — скрин-захват и sync работают."
    fi
    # OPTIONAL on-device transcription (heavy ~2 GB torch). Off by default;
    # the server transcribes uploaded audio instead. Only for users who
    # want transcription to never leave the Mac.
    if [ "$AUDIO_ENABLED" = "true" ] && [ "${{PERSONA_AGENT_WHISPER:-0}}" = "1" ]; then
        echo "→ Installing local Whisper (~2GB torch — долго)..."
        ./.venv/bin/pip install $PIP_OPTS \
            "silero-vad>=5.1" "openai-whisper>=20240930" \
            || echo "⚠ Локальный Whisper не встал — расшифровка будет на сервере."
    fi
fi

# 3. Config — TOML at the canonical path the agent actually reads
#    (~/.config/persona-agent.toml). device_token enables the T28
#    workspace-sync + heartbeat loops; omitted if the server didn't mint one.
echo "→ Writing config (~/.config/persona-agent.toml)..."
mkdir -p "$HOME/.config"
CONFIG_PATH="$HOME/.config/persona-agent.toml"
cat > "$CONFIG_PATH" <<EOF
[server]
url = "$SERVER_URL"
token = "$AGENT_TOKEN"
EOF
if [ -n "$DEVICE_TOKEN" ]; then
cat >> "$CONFIG_PATH" <<EOF
device_token = "$DEVICE_TOKEN"
EOF
fi
cat >> "$CONFIG_PATH" <<EOF

[capture]
screen = true
audio = $AUDIO_ENABLED

[logging]
level = "INFO"
EOF
chmod 600 "$CONFIG_PATH"

# 4. launchd auto-start
PLIST="$HOME/Library/LaunchAgents/com.swairit.persona-agent.plist"
echo "→ Installing launchd plist at $PLIST..."
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.swairit.persona-agent</string>
    <key>ProgramArguments</key>
    <array>
      <string>$REPO_DIR/mac-agent/.venv/bin/python</string>
      <string>$REPO_DIR/mac-agent/persona_agent.py</string>
      <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR/mac-agent</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/persona-agent.out.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/persona-agent.err.log</string>
  </dict>
</plist>
EOF

# Stop any existing instance, then load fresh.
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo ""
echo "✅ Done! Agent running and will auto-start at every login."
echo "   Logs: $HOME/Library/Logs/persona-agent.{{out,err}}.log"
echo "   Stop: launchctl unload $PLIST"
echo "   Open Settings → Privacy & Security → Screen Recording, allow Terminal/Python."
"""
    return PlainTextResponse(
        script,
        headers={
            "Content-Type": "text/x-shellscript; charset=utf-8",
            # Prevent caching — each token is single-use anyway, but
            # be explicit so corporate proxies don't store it.
            "Cache-Control": "no-store, max-age=0",
        },
    )
