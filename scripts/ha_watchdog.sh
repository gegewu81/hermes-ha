#!/bin/bash
# ha_watchdog.sh — Pi standby watchdog
# Runs on Pi via cron. Checks if WSL primary is alive.
# If primary is dead for THRESHOLD seconds, promote Pi to primary
# and start the Hermes gateway.
#
# Usage:
#   * * * * * /path/to/ha_watchdog.sh >> ~/.hermes/logs/ha_watchdog.log 2>&1

set -euo pipefail

HERMES_DIR="${HOME}/.hermes"
HA_DIR="${HERMES_DIR}/.ha"
HEARTBEAT_FILE="${HA_DIR}/heartbeat_primary"
NODE_FILE="${HA_DIR}/node.json"
EVENTS_LOG="${HA_DIR}/events.log"
LOG_FILE="${HERMES_DIR}/logs/ha_watchdog.log"

THRESHOLD=300  # 5 minutes without heartbeat = primary is dead
GRACE_PERIOD=600  # 10 min grace after initial setup

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${EVENTS_LOG}" 2>/dev/null || true
}

ensure_dirs() {
    mkdir -p "${HA_DIR}" "${HERMES_DIR}/logs"
}

get_role() {
    if [ -f "${NODE_FILE}" ]; then
        python3 -c "import json; print(json.load(open('${NODE_FILE}')).get('role','unknown'))" 2>/dev/null || echo "unknown"
    else
        echo "uninitialized"
    fi
}

set_role() {
    local role="$1"
    python3 -c "
import json
from datetime import datetime, timezone
p = '${NODE_FILE}'
try:
    d = json.load(open(p))
except:
    d = {}
d['role'] = '${role}'
d['hostname'] = __import__('os').uname().nodename
if 'initialized_at' not in d:
    d['initialized_at'] = datetime.now(timezone.utc).isoformat()
d['updated_at'] = datetime.now(timezone.utc).isoformat()
json.dump(d, open(p, 'w'), indent=2)
" 2>/dev/null || true
    log "Role set to ${role}"
}

is_gateway_running() {
    if [ -f "${HERMES_DIR}/gateway.pid" ]; then
        local pid
        pid=$(cat "${HERMES_DIR}/gateway.pid")
        kill -0 "${pid}" 2>/dev/null && return 0
    fi
    return 1
}

get_epoch() {
    local f="${HA_DIR}/epoch"
    if [ -f "${f}" ]; then
        cat "${f}"
    else
        echo "0"
    fi
}

set_epoch() {
    echo "$1" > "${HA_DIR}/epoch"
}

get_heartbeat_age() {
    if [ ! -f "${HEARTBEAT_FILE}" ]; then
        echo "9999999"  # No heartbeat ever = "very old"
        return
    fi
    local mtime
    mtime=$(stat -c %Y "${HEARTBEAT_FILE}" 2>/dev/null || echo "0")
    echo $(( $(date +%s) - mtime ))
}

promote_to_primary() {
    log "PROMOTING Pi to PRIMARY — WSL heartbeat expired"

    # Increment epoch to win split-brain
    local epoch
    epoch=$(get_epoch)
    epoch=$((epoch + 1))
    set_epoch "${epoch}"

    set_role "primary"

    # Start gateway in background
    log "Starting Hermes gateway..."
    nohup hermes gateway >> "${HERMES_DIR}/logs/gateway_watchdog.log" 2>&1 &
    local gw_pid=$!
    echo "${gw_pid}" > "${HERMES_DIR}/gateway.pid"
    log "Gateway started (PID=${gw_pid}, epoch=${epoch})"
}

main() {
    ensure_dirs

    local role
    role=$(get_role)

    # Only act if we're standby or uninitialized
    if [ "${role}" = "primary" ]; then
        # Already primary — just check gateway is running
        if ! is_gateway_running; then
            log "WARNING: We are primary but gateway is not running"
        fi
        return 0
    fi

    # Check heartbeat age
    local age
    age=$(get_heartbeat_age)

    if [ "${age}" -lt "${THRESHOLD}" ]; then
        # Primary is alive, all good
        return 0
    fi

    # Heartbeat expired
    if [ "${age}" -lt "${GRACE_PERIOD}" ] && [ ! -f "${HEARTBEAT_FILE}" ]; then
        # No heartbeat file at all — might just be first setup
        log "No heartbeat file yet, skipping (grace period)"
        return 0
    fi

    # Check if we already tried recently (debounce: don't promote more than once per 5 min)
    local promote_marker="${HA_DIR}/last_promote_attempt"
    if [ -f "${promote_marker}" ]; then
        local last_attempt
        last_attempt=$(stat -c %Y "${promote_marker}" 2>/dev/null || echo "0")
        if [ $(( $(date +%s) - last_attempt )) -lt 300 ]; then
            return 0  # Already attempted recently
        fi
    fi

    # Check if gateway is already running (maybe already promoted)
    if is_gateway_running; then
        # Update marker but don't restart
        touch "${promote_marker}"
        return 0
    fi

    # Promote!
    touch "${promote_marker}"
    promote_to_primary
}

main "$@"
