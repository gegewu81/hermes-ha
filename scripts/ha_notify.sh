#!/bin/bash
# Hermes HA Notify — send notifications via hermes chat
#
# Usage:
#   ./ha_notify.sh "message"              # Send message to user
#   ./ha_notify.sh --event takeover      # Send preset event message
#   ./ha_notify.sh --test                 # Test notification
#
# This script is a standalone utility. ha_sync.py v2 and ha_watchdog.sh v2
# call hermes chat directly, but this script provides a convenient wrapper
# for manual use and debugging.

export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"
NODE_FILE="$HOME/.hermes/.ha_node"
STATE_FILE="$HOME/.hermes/.ha_state"

# --- Get node name ---
NODE_NAME="unknown"
NODE_TYPE="unknown"
if [ -f "$NODE_FILE" ]; then
    NODE_NAME=$(python3 -c "import json; d=json.load(open('$NODE_FILE')); print(d.get('name','unknown'))" 2>/dev/null || echo "unknown")
    NODE_TYPE=$(python3 -c "import json; d=json.load(open('$NODE_FILE')); print(d.get('type','unknown'))" 2>/dev/null || echo "unknown")
fi

# --- Preset event messages ---
case "${1:-}" in
    --test)
        MESSAGE="🔔 HA Test: Notification from ${NODE_NAME} (${NODE_TYPE}). If you see this, notifications work!"
        ;;
    --event)
        EVENT="${2:-unknown}"
        ROLE=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('role','?'))" 2>/dev/null || echo "?")
        EPOCH=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('epoch','?'))" 2>/dev/null || echo "?")
        case "$EVENT" in
            takeover)
                MESSAGE="🔔 HA Takeover: ${NODE_NAME} → PRIMARY (epoch ${EPOCH}). Peer is STANDBY."
                ;;
            handoff)
                MESSAGE="🔔 HA Handoff: ${NODE_NAME} → OFFLINE. Peer → PRIMARY (epoch ${EPOCH})."
                ;;
            failover)
                MESSAGE="🔔 HA Failover: Peer offline. ${NODE_NAME} → PRIMARY (epoch ${EPOCH})."
                ;;
            recovery)
                MESSAGE="🔔 HA Recovery: Peer back online. ${NODE_NAME} → STANDBY."
                ;;
            *)
                MESSAGE="🔔 HA Event [${EVENT}]: ${NODE_NAME} (role=${ROLE}, epoch=${EPOCH})."
                ;;
        esac
        ;;
    --help|-h)
        echo "Usage: $0 [message|--event TYPE|--test|--help]"
        echo ""
        echo "Options:"
        echo "  message        Send custom message"
        echo "  --event TYPE   Send preset event (takeover|handoff|failover|recovery)"
        echo "  --test         Send test notification"
        echo "  --help         Show this help"
        exit 0
        ;;
    "")
        echo "Usage: $0 [message|--event TYPE|--test|--help]"
        exit 1
        ;;
    *)
        MESSAGE="$1"
        ;;
esac

# --- Send notification ---
echo "Sending: ${MESSAGE}"
hermes chat -q "${MESSAGE}" 2>&1
RC=$?

if [ $RC -eq 0 ]; then
    echo "OK: Notification sent successfully."
else
    echo "ERROR: Notification failed (exit code ${RC})."
    echo "Make sure 'hermes chat -q' works on this node."
    exit 1
fi
