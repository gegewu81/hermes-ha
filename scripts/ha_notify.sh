#!/bin/bash
# ha_notify.sh — User notification helper for HA events
# Sends notifications via available channels (terminal bell, notify-send, log)
#
# Usage:
#   ha_notify.sh "Primary failover detected"
#   ha_notify.sh --level warn "Pi unreachable"

set -euo pipefail

LEVEL="${1:-info}"
shift || true
MESSAGE="${*:-}"
if [ -z "${MESSAGE}" ]; then
    echo "Usage: ha_notify.sh [--level LEVEL] MESSAGE"
    exit 1
fi

# Parse --level flag
if [ "${LEVEL}" = "--level" ]; then
    LEVEL="${1:-info}"
    shift || true
    MESSAGE="${*:-}"
fi

HERMES_DIR="${HOME}/.hermes"
LOG_FILE="${HERMES_DIR}/logs/ha_notify.log"
HA_EVENTS="${HERMES_DIR}/.ha/events.log"

ensure_log() {
    mkdir -p "${HERMES_DIR}/logs" "${HERMES_DIR}/.ha"
}

log_to_file() {
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    local line="[${ts}] [${LEVEL^^}] ${MESSAGE}"
    echo "${line}" >> "${LOG_FILE}" 2>/dev/null || true
    echo "${line}" >> "${HA_EVENTS}" 2>/dev/null || true
}

bell() {
    printf '\a' 2>/dev/null || true
}

desktop_notify() {
    if command -v notify-send &>/dev/null && [ -n "${DISPLAY:-}" ]; then
        local icon="dialog-information"
        case "${LEVEL}" in
            error|crit) icon="dialog-error" ;;
            warn|warning) icon="dialog-warning" ;;
        esac
        notify-send -u "${LEVEL}" -i "${icon}" "Hermes HA" "${MESSAGE}" 2>/dev/null || true
    fi
}

main() {
    ensure_log
    log_to_file

    # Always bell for error/warn
    case "${LEVEL}" in
        error|warn|warning|crit)
            bell
            desktop_notify
            ;;
    esac

    # Print to stderr for cron visibility
    echo "[${LEVEL^^}] ${MESSAGE}" >&2
}

main "$@"
