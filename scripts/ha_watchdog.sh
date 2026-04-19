#!/bin/bash
# =============================================================================
# Hermes HA Watchdog v2 — runs on standby node via cron (every minute)
#
# Architecture:
#   Primary node (WSL) writes heartbeat timestamp to standby (Pi) every 2 min.
#   This watchdog checks heartbeat staleness to detect primary offline.
#
# Decision logic:
#   1. Read heartbeat file → compute age (seconds since last heartbeat)
#   2. If heartbeat fresh (< HEARTBEAT_THRESHOLD):
#      - If local role is "primary": demote to standby, stop gateway
#      - If local role is "standby": safety-net, stop gateway if running
#   3. If heartbeat stale (>= HEARTBEAT_THRESHOLD):
#      - If local role is "standby" and last_primary was peer: promote to primary
#      - If local role is "primary": ensure gateway is running
#
# v2 changes (2026-04-18):
#   - Node identity labels from .ha_node (same as ha_sync.py v2)
#   - Epoch counter: promote increments epoch, prevents split-brain
#   - Event logging to .ha_events.jsonl
#   - Separated role demote vs gateway stop (pitfall #22 fix)
#   - failover_reason field in state
#   - User notification via ha_notify.sh (if available)
#   - Added HA_WATCHDOG_SKIP env var for maintenance windows
# =============================================================================

set -euo pipefail

# --- Cron env fix (pitfall #23) ---
# Cron doesn't inherit DBUS/XDG from user session. Without these,
# systemctl --user silently fails (can't connect to user scope bus).
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# --- Config ---
export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"
HERMES_HOME="$HOME/.hermes"
STATE_FILE="$HERMES_HOME/.ha_state"
NODE_FILE="$HERMES_HOME/.ha_node"
HEARTBEAT_FILE="$HERMES_HOME/.ha_heartbeat"
EVENTS_FILE="$HERMES_HOME/.ha_events.jsonl"
NOTIFY_SCRIPT="$HERMES_HOME/skills/devops/agent-ha/scripts/ha_notify.sh"
LOG="$HERMES_HOME/logs/ha_watchdog.log"

HEARTBEAT_THRESHOLD="${HA_HEARTBEAT_STALE:-180}"  # seconds
MAX_EVENTS=1000

mkdir -p "$(dirname "$LOG")"

# Skip if maintenance window is active
if [ "${HA_WATCHDOG_SKIP:-}" = "1" ]; then
    exit 0
fi

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

# --- Node identity ---
NODE_NAME="unknown"
NODE_TYPE="unknown"
NODE_ARCH="unknown"
NODE_MODEL=""
if [ -f "$NODE_FILE" ]; then
    NODE_NAME=$(python3 -c "import json; d=json.load(open('$NODE_FILE')); print(d.get('name','unknown'))" 2>/dev/null || echo "unknown")
    NODE_TYPE=$(python3 -c "import json; d=json.load(open('$NODE_FILE')); print(d.get('type','unknown'))" 2>/dev/null || echo "unknown")
    NODE_ARCH=$(python3 -c "import json; d=json.load(open('$NODE_FILE')); print(d.get('arch','unknown'))" 2>/dev/null || echo "unknown")
    NODE_MODEL=$(python3 -c "import json; d=json.load(open('$NODE_FILE')); print(d.get('model',''))" 2>/dev/null || echo "")
fi

# Short label like [pi|RPi 4 Model B Rev 1.4|aarch64] or [wsl|x86_64]
if [ "$NODE_TYPE" = "pi" ] && [ -n "$NODE_MODEL" ]; then
    SHORT_MODEL=$(echo "$NODE_MODEL" | sed 's/Raspberry Pi /RPi /')
    NODE_LABEL="[$NODE_NAME|$SHORT_MODEL|$NODE_ARCH]"
else
    NODE_LABEL="[$NODE_NAME|$NODE_ARCH]"
fi

# Infer peer label (opposite of local)
if [ "$NODE_TYPE" = "pi" ]; then
    PEER_LABEL="WSL"
else
    PEER_LABEL="Pi"
fi

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $NODE_LABEL $1" >> "$LOG"
}

log_event() {
    local event_type="$1"
    local message="$2"
    local ts=$(date +%s)
    local time_str=$(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S')
    echo "{\"ts\":$ts,\"time\":\"$time_str\",\"node\":\"$NODE_NAME\",\"node_type\":\"$NODE_TYPE\",\"event\":\"$event_type\",\"message\":\"$message\"}" >> "$EVENTS_FILE"
    # Trim old events (keep last MAX_EVENTS)
    local count=$(wc -l < "$EVENTS_FILE" 2>/dev/null || echo 0)
    if [ "$count" -gt "$MAX_EVENTS" ]; then
        tail -n "$MAX_EVENTS" "$EVENTS_FILE" > "${EVENTS_FILE}.tmp" && mv "${EVENTS_FILE}.tmp" "$EVENTS_FILE"
    fi
}

notify_user() {
    local message="$1"
    if [ -x "$NOTIFY_SCRIPT" ]; then
        "$NOTIFY_SCRIPT" "$message" >> "$LOG" 2>&1 || true
    fi
}

# =============================================================================
# READ CURRENT STATE
# =============================================================================

NOW=$(date +%s)

# --- Heartbeat age ---
HEARTBEAT_AGE=9999  # default: very old (primary offline)
if [ -f "$HEARTBEAT_FILE" ]; then
    HB_TS=$(cat "$HEARTBEAT_FILE" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$HB_TS" ] && [ "$HB_TS" -gt 0 ] 2>/dev/null; then
        HEARTBEAT_AGE=$(( NOW - HB_TS ))
    fi
fi

# --- HA state ---
ROLE=""
LAST_PRIMARY=""
CURRENT_EPOCH=0
if [ -f "$STATE_FILE" ]; then
    ROLE=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('role',''))" 2>/dev/null || echo "")
    LAST_PRIMARY=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('last_primary',''))" 2>/dev/null || echo "")
    CURRENT_EPOCH=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('epoch',0))" 2>/dev/null || echo "0")
fi

# --- Gateway status ---
GW_RUNNING="NO"
if systemctl --user is-active hermes-gateway.service >/dev/null 2>&1; then
    GW_RUNNING="YES"
fi

# --- Is primary (peer) online? ---
PEER_ONLINE=false
if [ "$HEARTBEAT_AGE" -lt "$HEARTBEAT_THRESHOLD" ] 2>/dev/null; then
    PEER_ONLINE=true
fi

# =============================================================================
# DECISION LOGIC
# =============================================================================

if $PEER_ONLINE; then
    # =====================================================================
    # PEER IS ONLINE (heartbeat fresh)
    # =====================================================================

    if [ "$ROLE" = "primary" ]; then
        # We are primary but peer just came back online → demote to standby
        log "$PEER_LABEL online (heartbeat ${HEARTBEAT_AGE}s). Demoting from PRIMARY to STANDBY."

        # Write standby state with current epoch (do NOT increment epoch on demote)
        python3 -c "
import json, time
state = {
    'role': 'standby',
    'last_sync': int(time.time()),
    'last_primary': '$( [ "$NODE_TYPE" = "pi" ] && echo "wsl" || echo "pi" )',
    'node': '$NODE_NAME',
    'node_type': '$NODE_TYPE',
    'epoch': $CURRENT_EPOCH
}
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null

        log_event "info" "$PEER_LABEL heartbeat fresh (${HEARTBEAT_AGE}s). $NODE_LABEL demoted to STANDBY (epoch $CURRENT_EPOCH)."
        notify_user "HA: $NODE_LABEL demoted to STANDBY, $PEER_LABEL is back online (epoch $CURRENT_EPOCH)"

        # Stop gateway (role changed)
        if [ "$GW_RUNNING" = "YES" ]; then
            systemctl --user stop hermes-gateway.service
            log "Gateway stopped (demoted to standby)"
        fi

    elif [ "$ROLE" = "standby" ]; then
        # Already standby — just safety-net: ensure gateway is NOT running
        if [ "$GW_RUNNING" = "YES" ]; then
            log "$PEER_LABEL online (heartbeat ${HEARTBEAT_AGE}s), we are STANDBY but gateway running. Stopping gateway."
            systemctl --user stop hermes-gateway.service
            log_event "info" "Safety-net: stopped gateway while $NODE_LABEL is STANDBY and $PEER_LABEL is online"
        fi
        # else: everything is normal, nothing to do
    fi

else
    # =====================================================================
    # PEER IS OFFLINE (heartbeat stale or missing)
    # =====================================================================

    if [ "$ROLE" = "primary" ]; then
        # Already primary — just ensure gateway is running
        if [ "$GW_RUNNING" = "NO" ]; then
            log "$NODE_LABEL is PRIMARY (epoch $CURRENT_EPOCH, no $PEER_LABEL heartbeat for ${HEARTBEAT_AGE}s). Starting gateway..."
            systemctl --user start hermes-gateway.service
            log_event "info" "Gateway start attempted (already primary, epoch $CURRENT_EPOCH, GW was down)"

            # Verify it started
            sleep 2
            if systemctl --user is-active hermes-gateway.service >/dev/null 2>&1; then
                log "Gateway started successfully"
            else
                log "WARNING: Gateway failed to start (will retry next cycle)"
                log_event "error" "Gateway start FAILED (epoch $CURRENT_EPOCH)"
            fi
        fi

    elif [ "$ROLE" = "standby" ] && [ "$LAST_PRIMARY" != "$NODE_NAME" ]; then
        # We are standby, peer was primary but is now offline → PROMOTE
        NEW_EPOCH=$(( CURRENT_EPOCH + 1 ))

        log "$PEER_LABEL OFFLINE (no heartbeat for ${HEARTBEAT_AGE}s). Promoting $NODE_LABEL to PRIMARY (epoch $NEW_EPOCH)..."

        # Write primary state with incremented epoch
        python3 -c "
import json, time
state = {
    'role': 'primary',
    'last_sync': int(time.time()),
    'last_primary': '$NODE_NAME',
    'node': '$NODE_NAME',
    'node_type': '$NODE_TYPE',
    'epoch': $NEW_EPOCH,
    'failover_reason': 'heartbeat_timeout'
}
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null

        log_event "failover" "$PEER_LABEL offline for ${HEARTBEAT_AGE}s, $NODE_LABEL promoted to PRIMARY (epoch $NEW_EPOCH)"

        # Start gateway FIRST — notify needs gateway's WebSocket to deliver
        if [ "$GW_RUNNING" = "NO" ]; then
            systemctl --user start hermes-gateway.service
            sleep 5
            if systemctl --user is-active hermes-gateway.service >/dev/null 2>&1; then
                log "$NODE_LABEL promoted to PRIMARY (epoch $NEW_EPOCH). Gateway started."
            else
                log "WARNING: $NODE_LABEL promoted to PRIMARY (epoch $NEW_EPOCH) but gateway FAILED to start"
                log_event "error" "Gateway start FAILED after promote (epoch $NEW_EPOCH)"
            fi
        else
            log "$NODE_LABEL promoted to PRIMARY (epoch $NEW_EPOCH). Gateway already running."
        fi

        # Notify AFTER gateway is up — hermes chat -q requires active WebSocket
        sleep 3
        notify_user "⚠️ HA FAILOVER: $PEER_LABEL offline (${HEARTBEAT_AGE}s), $NODE_LABEL is now PRIMARY (epoch $NEW_EPOCH)"

    elif [ "$ROLE" = "standby" ] && [ "$LAST_PRIMARY" = "$NODE_NAME" ]; then
        # Edge case: we are standby but last_primary was us (e.g. after a handoff that
        # didn't complete cleanly). Peer is offline. We should promote.
        NEW_EPOCH=$(( CURRENT_EPOCH + 1 ))
        log "$PEER_LABEL OFFLINE (no heartbeat for ${HEARTBEAT_AGE}s). last_primary=$LAST_PRIMARY. Promoting (epoch $NEW_EPOCH)..."

        python3 -c "
import json, time
state = {
    'role': 'primary',
    'last_sync': int(time.time()),
    'last_primary': '$NODE_NAME',
    'node': '$NODE_NAME',
    'node_type': '$NODE_TYPE',
    'epoch': $NEW_EPOCH,
    'failover_reason': 'heartbeat_timeout'
}
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null

        log_event "failover" "$PEER_LABEL offline for ${HEARTBEAT_AGE}s, $NODE_LABEL promoted to PRIMARY (epoch $NEW_EPOCH, last_primary was self)"

        if [ "$GW_RUNNING" = "NO" ]; then
            systemctl --user start hermes-gateway.service
            sleep 5
            if systemctl --user is-active hermes-gateway.service >/dev/null 2>&1; then
                log "$NODE_LABEL promoted to PRIMARY (epoch $NEW_EPOCH). Gateway started after promote."
            else
                log "WARNING: Gateway FAILED to start after promote (epoch $NEW_EPOCH)"
                log_event "error" "Gateway start FAILED after promote (epoch $NEW_EPOCH)"
            fi
        else
            log "$NODE_LABEL promoted to PRIMARY (epoch $NEW_EPOCH). Gateway already running."
        fi

        sleep 3
        notify_user "⚠️ HA FAILOVER: $NODE_LABEL promoted to PRIMARY (epoch $NEW_EPOCH)"

    else
        # Unknown state — log for debugging
        log "Unknown state: role=$ROLE, last_primary=$LAST_PRIMARY, epoch=$CURRENT_EPOCH, heartbeat_age=${HEARTBEAT_AGE}s"
    fi
fi
