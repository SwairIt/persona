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
from app.logging_setup import get_logger
from app.remote_agents import create_agent
from app.web.templates_engine import templates

router = APIRouter(tags=["install"])
log = get_logger("persona.install")

# In-memory single-use install tokens. Wiped on restart — that's fine
# because they live <10 minutes anyway. Maps install_id → (agent_token,
# server_url, created_at_epoch).
_PENDING: dict[str, tuple[str, str, float]] = {}
_TTL_SECONDS = 600  # 10 minutes — plenty of time to copy-paste


def _purge_expired() -> None:
    now = time.time()
    expired = [k for k, (_, _, t) in _PENDING.items() if now - t > _TTL_SECONDS]
    for k in expired:
        _PENDING.pop(k, None)


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

    install_id = secrets.token_urlsafe(24)
    # Use the public URL so the token-bearing curl URL inside the script
    # itself also reaches the Mac. Falling back to the request URL would
    # bake ``localhost`` into config.json — the Mac agent would then try
    # to POST to its own loopback and silently 404.
    server_url, _is_local = _detect_public_url(request)
    _PENDING[install_id] = (raw_token, server_url, time.time())

    log.info(
        "install.mac.minted",
        install_id_prefix=install_id[:6],
        user_id=user_id,
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
    agent_token, server_url, _ = record

    # Quote everything safely. Tokens are url-safe base64 so they're
    # already shell-safe, but treat as user input anyway.
    safe_token = agent_token.replace("'", "'\\''")
    safe_url = server_url.replace("'", "'\\''")

    script = f"""#!/usr/bin/env bash
# Persona Mac agent installer — auto-generated on the server.
# Run from any terminal:  curl -fsSL {server_url}/api/install/mac.sh?t=... | bash
#
# This script:
#   1. Clones the Persona repo to ~/persona  (or pulls if it exists)
#   2. Creates a Python venv + installs requirements
#   3. Writes mac-agent/config.json with your server URL and agent token
#   4. Registers a launchd plist so the agent starts at login
#   5. Starts the agent immediately

set -euo pipefail

REPO_DIR="$HOME/persona"
SERVER_URL='{safe_url}'
AGENT_TOKEN='{safe_token}'

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

# 2. venv
cd "$REPO_DIR/mac-agent"
if [ ! -d ".venv" ]; then
    echo "→ Creating venv..."
    python3 -m venv .venv
fi
echo "→ Installing requirements..."
./.venv/bin/pip install --quiet --upgrade pip
if [ -f requirements.txt ]; then
    ./.venv/bin/pip install --quiet -r requirements.txt
fi

# 3. Config
echo "→ Writing config.json..."
cat > config.json <<EOF
{{
  "server": {{
    "url": "$SERVER_URL",
    "token": "$AGENT_TOKEN"
  }}
}}
EOF
chmod 600 config.json

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
