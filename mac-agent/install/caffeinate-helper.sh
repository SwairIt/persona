#!/usr/bin/env bash
# caffeinate-helper.sh -- keep the Mac awake while persona-agent is running.
#
# By default macOS will sleep when the lid closes, which kills the LaunchAgent.
# This helper wraps `caffeinate -d -i -s` (or `-i -m` if the user passes
# --battery-ok) and, when used together with persona-agent, ensures the Mac
# stays awake long enough to keep streaming.
#
# Usage patterns:
#
#   1) Wrap the agent directly (simplest):
#        caffeinate-helper.sh -- persona-agent run --config ~/.config/persona-agent.toml
#      caffeinate exits when persona-agent exits.
#
#   2) Run alongside an existing LaunchAgent (background):
#        nohup caffeinate-helper.sh --pid "$(pgrep -f persona-agent)" >/dev/null 2>&1 &
#      caffeinate exits as soon as the watched PID disappears.
#
#   3) Run forever (until ^C / kill):
#        caffeinate-helper.sh --forever
#
# Flags:
#   --battery-ok   Allow display sleep & don't prevent idle sleep on battery
#                  (just delays system sleep). Quieter on the fan.
#   --pid <PID>    Tie lifetime to an external PID instead of a child command.
#   --forever      Stay caffeinated until this script is killed.
#   -- <cmd...>    Run <cmd...> as a child; caffeinate lives as long as it does.
#
# Notes:
#   - caffeinate is a stock macOS binary; no install needed.
#   - This script is OPTIONAL. The LaunchAgent works without it as long as the
#     Mac stays awake by other means (lid open, plugged in with "prevent sleep
#     when display is off" enabled in Energy Saver, etc).

set -euo pipefail

BATTERY_OK=0
PID_TO_WATCH=""
MODE=""
CHILD_CMD=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --battery-ok) BATTERY_OK=1; shift;;
        --pid) PID_TO_WATCH="$2"; MODE="pid"; shift 2;;
        --pid=*) PID_TO_WATCH="${1#*=}"; MODE="pid"; shift;;
        --forever) MODE="forever"; shift;;
        --) shift; CHILD_CMD=("$@"); MODE="child"; break;;
        -h|--help) sed -n '2,32p' "$0"; exit 0;;
        *) echo "unknown argument: $1" >&2; exit 2;;
    esac
done

if [[ -z "${MODE}" ]]; then
    echo "caffeinate-helper: choose one of --pid <PID>, --forever, or -- <cmd...>" >&2
    exit 2
fi

# Flag set:
#   -d  prevent display sleep
#   -i  prevent idle system sleep
#   -m  prevent disk sleep (only with --battery-ok = off)
#   -s  prevent system sleep when on AC power
CAFFEINATE_FLAGS=(-i)
if (( BATTERY_OK == 0 )); then
    CAFFEINATE_FLAGS+=(-d -m -s)
fi

case "${MODE}" in
    child)
        if [[ ${#CHILD_CMD[@]} -eq 0 ]]; then
            echo "caffeinate-helper: -- needs a command to run" >&2
            exit 2
        fi
        # -w would tie caffeinate to a PID, but here we want it to live exactly
        # as long as our child command does. Easiest: exec via caffeinate's
        # passthrough form.
        exec caffeinate "${CAFFEINATE_FLAGS[@]}" "${CHILD_CMD[@]}"
        ;;
    pid)
        if ! kill -0 "${PID_TO_WATCH}" 2>/dev/null; then
            echo "caffeinate-helper: PID ${PID_TO_WATCH} is not running" >&2
            exit 1
        fi
        exec caffeinate "${CAFFEINATE_FLAGS[@]}" -w "${PID_TO_WATCH}"
        ;;
    forever)
        # `caffeinate -t 0` is a no-op; just run with no timeout and wait for
        # signals.
        exec caffeinate "${CAFFEINATE_FLAGS[@]}"
        ;;
esac
