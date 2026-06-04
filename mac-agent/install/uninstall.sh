#!/usr/bin/env bash
# Persona Mac agent uninstaller.
#
# Reverses install.sh:
#   - launchctl bootout the LaunchAgent and remove the plist
#   - optionally remove the venv at ~/.persona-agent
#   - optionally remove the config at ~/.config/persona-agent.toml
#   - optionally remove the log files
#   - optionally remove the /usr/local/bin/persona-agent symlink
#
# Usage:
#   bash uninstall.sh                  # interactive (asks before deleting data)
#   bash uninstall.sh --purge          # delete venv + config + logs without prompting
#   bash uninstall.sh --keep-config    # only remove the LaunchAgent, leave data
#
# Safe to run when nothing is installed -- missing pieces are skipped quietly.

set -euo pipefail

LABEL="com.persona.agent"
AGENT_HOME="${HOME}/.persona-agent"
CONFIG_PATH="${HOME}/.config/persona-agent.toml"
PLIST_DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_OUT="${HOME}/Library/Logs/persona-agent.log"
LOG_ERR="${HOME}/Library/Logs/persona-agent.err"
SYMLINK_TARGET="/usr/local/bin/persona-agent"

log()  { printf '\033[1;34m[persona]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[persona]\033[0m %s\n' "$*" >&2; }

PURGE=0
KEEP_CONFIG=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge) PURGE=1; shift;;
        --keep-config) KEEP_CONFIG=1; shift;;
        -h|--help) sed -n '2,18p' "$0"; exit 0;;
        *) warn "ignoring unknown argument: $1"; shift;;
    esac
done

if (( PURGE && KEEP_CONFIG )); then
    warn "--purge and --keep-config are mutually exclusive; --purge wins"
    KEEP_CONFIG=0
fi

UID_NUM="$(id -u)"
SERVICE_TARGET="gui/${UID_NUM}/${LABEL}"

# -----------------------------------------------------------------------------
# Stop & remove LaunchAgent
# -----------------------------------------------------------------------------
if launchctl print "${SERVICE_TARGET}" >/dev/null 2>&1; then
    log "Stopping LaunchAgent ${SERVICE_TARGET}..."
    launchctl bootout "${SERVICE_TARGET}" 2>/dev/null || \
        warn "launchctl bootout returned non-zero; agent may have already exited"
else
    log "LaunchAgent ${SERVICE_TARGET} is not loaded; skipping bootout."
fi

if [[ -f "${PLIST_DEST}" ]]; then
    rm -f "${PLIST_DEST}"
    log "Removed ${PLIST_DEST}"
fi

# -----------------------------------------------------------------------------
# Symlink
# -----------------------------------------------------------------------------
if [[ -L "${SYMLINK_TARGET}" ]]; then
    if [[ -w "${SYMLINK_TARGET}" ]] || [[ -w "$(dirname "${SYMLINK_TARGET}")" ]]; then
        rm -f "${SYMLINK_TARGET}"
        log "Removed ${SYMLINK_TARGET}"
    else
        warn "Cannot remove ${SYMLINK_TARGET} without sudo; run: sudo rm ${SYMLINK_TARGET}"
    fi
fi

# -----------------------------------------------------------------------------
# Data: venv + config + logs
# -----------------------------------------------------------------------------
prompt_yes() {
    local question="$1"
    if (( PURGE )); then return 0; fi
    if (( KEEP_CONFIG )); then return 1; fi
    if [[ ! -t 0 ]]; then return 1; fi  # non-interactive default = keep
    read -r -p "${question} [y/N] " answer
    [[ "${answer}" =~ ^[Yy]$ ]]
}

if [[ -d "${AGENT_HOME}" ]] && prompt_yes "Delete virtualenv at ${AGENT_HOME}?"; then
    rm -rf "${AGENT_HOME}"
    log "Removed ${AGENT_HOME}"
fi

if [[ -f "${CONFIG_PATH}" ]] && prompt_yes "Delete config ${CONFIG_PATH} (contains pairing token)?"; then
    rm -f "${CONFIG_PATH}"
    log "Removed ${CONFIG_PATH}"
fi

if { [[ -f "${LOG_OUT}" ]] || [[ -f "${LOG_ERR}" ]]; } && prompt_yes "Delete log files in ~/Library/Logs/?"; then
    rm -f "${LOG_OUT}" "${LOG_ERR}"
    log "Removed persona-agent logs"
fi

log "Uninstall complete."
log "Reminder: you may also want to revoke this agent's token at /admin/agents on the server."
