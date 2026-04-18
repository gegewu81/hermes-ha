#!/bin/bash
# Hermes HA Watchdog v2 — runs on Pi via cron (every minute)
#
# Failover logic:
#   1. Check heartbeat file (written by WSL every 2 min)
#   2. If heartbeat is fresh (< 3 min old): WSL is online → Pi should be standby
#   3. If heartbeat is stale or missing: WSL is offline → Pi promotes to primary
#   4. Epoch awareness: on promote, read current epoch and increment
#   5. Node identity: all log output includes node label
#
# v2 changes (2026-04-18):
#   - Epoch counter: promote increments epoch, prevents split-brain
#   - Node identity labels in all output
#   - Event logging via .ha_events.jsonl
#   - State file includes node/epoch fields

export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"
HERMES_HOME="$HOME/.hermes"
STATE_FILE="$HERMES_HOME/.ha_state"
NODE_FILE="$HERMES_HOME/.ha_node"
HEARTBEAT_FILE="$HERMES_HOME/.ha_heartbeat"
EVENTS_FILE="$HERMES_HOME/.ha_events.jsonl"
LOG="$HERMES_HOME/logs/ha_watchdog.log"

mkdir -p "$(dirname "$LOG")"

# --- Node identity ---
NODE_NAME="unknown"
NODE_TYPE="unknown"
if [ -f "$NODE_FILE" ]; then
    NODE_NAME=$(python3 -c "import json; d=json.load(open('$NODE_FILE')); print(d.get('name','unknown'))" 2>/dev/null || echo "unknown")
    NODE_TYPE=$(python3 -c "import json; d=json.load(open('$NODE_FILE')); print(d.get('type','unknown'))" 2>/dev/null || echo "unknown")
fi

log_event() {
    local event_type="$1"
    local message="$2"
    local ts=$(date +%s)
    local time_str=$(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S')
    echo "{\"ts\":$ts,\"time\":\"$time_str\",\"node\":\"$NODE_NAME\",\"node_type\":\"$NODE_TYPE\",\"event\":\"$event_type\",\"message\":\"$message\"}" >> "$EVENTS_FILE"
}

# --- Read heartbeat age ---
NOW=$(date +%s)
HEARTBEAT_AGE=9999  # default: very old (WSL offline)
if [ -f "$HEARTBEAT_FILE" ]; then
    HB_TS=$(cat "$HEARTBEAT_FILE" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$HB_TS" ] && [ "$HB_TS" -gt 0 ] 2>/dev/null; then
        HEARTBEAT_AGE=$(( NOW - HB_TS ))
    fi
fi

# --- Read HA state ---
ROLE=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('role',''))" 2>/dev/null || echo "")
LAST_PRIMARY=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('last_primary',''))" 2>/dev/null || echo "")
CURRENT_EPOCH=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('epoch',0))" 2>/dev/null || echo "0")

# --- Determine if WSL is online ---
WSL_ONLINE=false
HEARTBEAT_THRESHOLD=${HA_HEARTBEAT_STALE:-180}
if [ "$HEARTBEAT_AGE" -lt "$HEARTBEAT_THRESHOLD" ] 2>/dev/null; then
    WSL_ONLINE=true
fi

# --- Check gateway status ---
GW_RUNNING=$(systemctl --user is-active hermes-gateway.service 2>/dev/null | grep -q active && echo YES || echo NO)

# --- Decision logic ---
if $WSL_ONLINE; then
    # WSL is online → Pi should be standby
    if [ "$GW_RUNNING" = "YES" ]; then
        echo "$(date) [$NODE_NAME|$NODE_TYPE] WSL online (heartbeat ${HEARTBEAT_AGE}s old). Stopping Pi gateway..." >> "$LOG"
        systemctl --user stop hermes-gateway.service
        # Update state to standby (keep epoch)
        python3 -c "
import json, time
state = {
    'role': 'standby',
    'last_sync': int(time.time()),
    'last_primary': 'wsl',
    'node': '$NODE_NAME',
    'node_type': '$NODE_TYPE',
    'epoch': $CURRENT_EPOCH
}
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null
        log_event "info" "Pi gateway stopped, WSL heartbeat fresh (${HEARTBEAT_AGE}s). Pi is STANDBY."
        echo "$(date) [$NODE_NAME|$NODE_TYPE] Pi gateway stopped. Pi is STANDBY." >> "$LOG"
    fi
else
    # WSL is offline (heartbeat stale/missing) → check if we should promote
    if [ "$ROLE" = "primary" ] || [ "$LAST_PRIMARY" = "pi" ]; then
        # Already primary, just ensure gateway is running
        if [ "$GW_RUNNING" = "NO" ]; then
            echo "$(date) [$NODE_NAME|$NODE_TYPE] Pi is PRIMARY (epoch $CURRENT_EPOCH, no WSL heartbeat for ${HEARTBEAT_AGE}s). Starting gateway..." >> "$LOG"
            systemctl --user start hermes-gateway.service
            log_event "info" "Gateway start attempted (already primary, epoch $CURRENT_EPOCH, GW was down)"
            echo "$(date) [$NODE_NAME|$NODE_TYPE] Gateway start attempted." >> "$LOG"
        fi
    else
        # WSL was last primary but heartbeat is stale → promote Pi
        NEW_EPOCH=$(( CURRENT_EPOCH + 1 ))
        echo "$(date) [$NODE_NAME|$NODE_TYPE] WSL OFFLINE (no heartbeat for ${HEARTBEAT_AGE}s). Promoting Pi to PRIMARY (epoch $NEW_EPOCH)..." >> "$LOG"
        # Update state with incremented epoch
        python3 -c "
import json, time
state = {
    'role': 'primary',
    'last_sync': int(time.time()),
    'last_primary': 'pi',
    'node': '$NODE_NAME',
    'node_type': '$NODE_TYPE',
    'epoch': $NEW_EPOCH,
    'failover_reason': 'heartbeat_timeout'
}
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null
        log_event "failover" "WSL offline for ${HEARTBEAT_AGE}s, Pi promoted to PRIMARY (epoch $NEW_EPOCH)"
        if [ "$GW_RUNNING" = "NO" ]; then
            systemctl --user start hermes-gateway.service
            echo "$(date) [$NODE_NAME|$NODE_TYPE] Pi promoted to PRIMARY (epoch $NEW_EPOCH). Gateway started." >> "$LOG"
        else
            echo "$(date) [$NODE_NAME|$NODE_TYPE] Pi promoted to PRIMARY (epoch $NEW_EPOCH). Gateway already running." >> "$LOG"
        fi
    fi
fi
