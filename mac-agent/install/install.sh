#!/usr/bin/env bash
# Persona Mac agent installer.
#
# Steps:
#   1. Verify Python 3.11+ is on PATH.
#   2. Create ~/.persona-agent/venv and `pip install -e ../` into it.
#   3. Collect server URL + pairing token (flags or interactive prompt).
#   4. Write ~/.config/persona-agent.toml.
#   5. Render com.persona.agent.plist into ~/Library/LaunchAgents/.
#   6. launchctl bootstrap the agent into the current GUI session.
#   7. Point the user at System Settings -> Privacy & Security -> Screen Recording.
#   8. Print verification commands.
#
# Usage:
#   bash install.sh
#   bash install.sh --server https://persona.example.com --token PA-xxxxxxxx
#   bash install.sh --server ... --token ... --non-interactive
#
# Idempotent: re-running upgrades the venv, rewrites the config (preserving
# unknown keys is NOT attempted -- back up your config first if you hand-edited
# it), and reloads the LaunchAgent.

set -euo pipefail

# -----------------------------------------------------------------------------
# Constants & helpers
# -----------------------------------------------------------------------------
LABEL="com.persona.agent"
AGENT_HOME="${HOME}/.persona-agent"
VENV_DIR="${AGENT_HOME}/venv"
CONFIG_DIR="${HOME}/.config"
CONFIG_PATH="${CONFIG_DIR}/persona-agent.toml"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_DEST="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"
LOG_DIR="${HOME}/Library/Logs"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_TEMPLATE="${SCRIPT_DIR}/com.persona.agent.plist"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

log()  { printf '\033[1;34m[persona]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[persona]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[persona]\033[0m %s\n' "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------
SERVER_URL=""
PAIR_TOKEN=""
NON_INTERACTIVE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server) SERVER_URL="$2"; shift 2;;
        --server=*) SERVER_URL="${1#*=}"; shift;;
        --token) PAIR_TOKEN="$2"; shift 2;;
        --token=*) PAIR_TOKEN="${1#*=}"; shift;;
        --non-interactive) NON_INTERACTIVE=1; shift;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *) die "unknown argument: $1";;
    esac
done

# -----------------------------------------------------------------------------
# Sanity checks
# -----------------------------------------------------------------------------
if [[ "$(uname -s)" != "Darwin" ]]; then
    die "this installer only runs on macOS (uname says: $(uname -s))"
fi

MACOS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
if (( MACOS_MAJOR < 12 )); then
    warn "macOS ${MACOS_MAJOR} detected; Persona supports macOS 12 (Monterey) and newer."
fi

log "Checking Python 3.11+..."
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
        version="$("${candidate}" -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))')"
        major="${version%%.*}"
        minor="${version#*.}"
        if [[ "${major}" == "3" ]] && (( minor >= 11 )); then
            PYTHON_BIN="$(command -v "${candidate}")"
            log "  -> using ${PYTHON_BIN} (Python ${version})"
            break
        fi
    fi
done
[[ -n "${PYTHON_BIN}" ]] || die "Python 3.11+ not found. Install via Homebrew: brew install python@3.12"

# -----------------------------------------------------------------------------
# Virtualenv + package install
# -----------------------------------------------------------------------------
log "Creating virtualenv at ${VENV_DIR}..."
mkdir -p "${AGENT_HOME}"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

VENV_PIP="${VENV_DIR}/bin/pip"
VENV_PERSONA="${VENV_DIR}/bin/persona-agent"

log "Upgrading pip + wheel inside venv..."
"${VENV_PIP}" install --upgrade pip wheel >/dev/null

log "Installing persona-agent from ${PACKAGE_DIR} ..."
"${VENV_PIP}" install -e "${PACKAGE_DIR}"

if [[ ! -x "${VENV_PERSONA}" ]]; then
    die "persona-agent entry point missing after install (${VENV_PERSONA})"
fi

# Symlink into /usr/local/bin if writable; otherwise fall back to the venv path.
SYMLINK_TARGET="/usr/local/bin/persona-agent"
AGENT_BIN="${VENV_PERSONA}"
if [[ -w "/usr/local/bin" ]] || sudo -n true 2>/dev/null; then
    if [[ -w "/usr/local/bin" ]]; then
        ln -sf "${VENV_PERSONA}" "${SYMLINK_TARGET}"
    else
        sudo ln -sf "${VENV_PERSONA}" "${SYMLINK_TARGET}"
    fi
    AGENT_BIN="${SYMLINK_TARGET}"
    log "Symlinked persona-agent -> ${AGENT_BIN}"
else
    warn "/usr/local/bin not writable; using ${AGENT_BIN} directly (no sudo prompt issued)"
fi

# -----------------------------------------------------------------------------
# Collect server URL + token
# -----------------------------------------------------------------------------
if [[ -z "${SERVER_URL}" ]]; then
    if (( NON_INTERACTIVE )); then
        die "--server is required in --non-interactive mode"
    fi
    read -r -p "Persona server URL (e.g. https://persona.example.com): " SERVER_URL
fi
[[ -n "${SERVER_URL}" ]] || die "server URL is required"

if [[ -z "${PAIR_TOKEN}" ]]; then
    if (( NON_INTERACTIVE )); then
        die "--token is required in --non-interactive mode"
    fi
    echo "Pair this Mac at ${SERVER_URL%/}/admin/agents to obtain a token."
    read -r -s -p "Pairing token: " PAIR_TOKEN
    echo
fi
[[ -n "${PAIR_TOKEN}" ]] || die "pairing token is required"

# Strip a trailing slash so concatenation in code is predictable.
SERVER_URL="${SERVER_URL%/}"

# -----------------------------------------------------------------------------
# Write config
# -----------------------------------------------------------------------------
log "Writing config -> ${CONFIG_PATH}"
mkdir -p "${CONFIG_DIR}"
HOSTNAME_DEFAULT="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
umask 077
cat >"${CONFIG_PATH}" <<EOF
# Persona Mac agent configuration.
# Generated by mac-agent/install/install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ).
# Permissions: 0600 -- this file contains a bearer token.

[server]
url   = "${SERVER_URL}"
token = "${PAIR_TOKEN}"

[agent]
hostname = "${HOSTNAME_DEFAULT}"

[capture]
# Compressed audio segments + WebP screenshots are uploaded to the server.
# Disable either source here if you want to opt out locally.
audio  = true
screen = true

[logging]
level = "INFO"
EOF
chmod 600 "${CONFIG_PATH}"

# -----------------------------------------------------------------------------
# Render & install LaunchAgent
# -----------------------------------------------------------------------------
log "Installing LaunchAgent -> ${PLIST_DEST}"
mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOG_DIR}"

# Escape & for sed replacement (paths don't usually contain it, but be safe).
escape_sed() { printf '%s' "$1" | sed -e 's/[&/\\]/\\&/g'; }

tmp_plist="$(mktemp -t persona-agent-plist)"
sed \
    -e "s|@@PERSONA_AGENT_BIN@@|$(escape_sed "${AGENT_BIN}")|g" \
    -e "s|@@PERSONA_HOME@@|$(escape_sed "${HOME}")|g" \
    -e "s|@@PERSONA_CONFIG@@|$(escape_sed "${CONFIG_PATH}")|g" \
    "${PLIST_TEMPLATE}" >"${tmp_plist}"

# Validate before installing; otherwise launchctl will refuse with a vague error.
plutil -lint "${tmp_plist}" >/dev/null || die "rendered plist failed plutil lint: ${tmp_plist}"

mv "${tmp_plist}" "${PLIST_DEST}"
chmod 644 "${PLIST_DEST}"

UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
SERVICE_TARGET="${DOMAIN}/${LABEL}"

# Bootout if already loaded so we can re-bootstrap with the fresh plist.
if launchctl print "${SERVICE_TARGET}" >/dev/null 2>&1; then
    log "Reloading existing LaunchAgent..."
    launchctl bootout "${SERVICE_TARGET}" 2>/dev/null || true
fi

log "Bootstrapping LaunchAgent into ${DOMAIN}..."
launchctl bootstrap "${DOMAIN}" "${PLIST_DEST}"
launchctl enable "${SERVICE_TARGET}" || true
launchctl kickstart -k "${SERVICE_TARGET}" || true

# -----------------------------------------------------------------------------
# Privacy prompts
# -----------------------------------------------------------------------------
log "Opening System Settings -> Privacy & Security -> Screen Recording..."
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" || \
    warn "could not auto-open Settings; navigate manually."

cat <<'EOF'

────────────────────────────────────────────────────────────────────────────
  Grant the following permissions, then restart the agent if needed:

    Privacy & Security -> Screen Recording   -> enable persona-agent
    Privacy & Security -> Microphone         -> enable persona-agent
    Privacy & Security -> Accessibility      -> enable persona-agent (optional)

  On first capture macOS may prompt again -- click "Allow" each time, then:

      launchctl kickstart -k gui/$(id -u)/com.persona.agent

  Verify the agent is reporting in:

      persona-agent status
      tail -f ~/Library/Logs/persona-agent.log

  Pause / resume:

      persona-agent pause     # stop uploading until next `resume`
      persona-agent resume

  Uninstall:

      bash install/uninstall.sh
────────────────────────────────────────────────────────────────────────────
EOF

log "Install complete."
