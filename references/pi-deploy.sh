#!/bin/bash
# Pi one-shot deployment script for Hermes HA
# Run from WSL: ssh -o BatchMode=yes pi 'bash -s' < pi-deploy.sh
# Or copy to Pi and run locally.

set -e

PI_HERMES_HOME="$HOME/.hermes"
PI_CONFIG_DIR="$HOME/.config/systemd/user"

echo "=== Hermes HA Pi Deployment ==="

# 1. Create logs directory
mkdir -p "$PI_HERMES_HOME/logs"

# 2. Deploy watchdog script (heartbeat-based failover)
cat > "$HOME/ha_watchdog.sh" << 'WATCHDOG_EOF'
#!/bin/bash
# Hermes HA Watchdog — runs on Pi via cron (every minute)
#
# Failover logic:
#   1. Check heartbeat file (written by WSL every 2 min)
#   2. If heartbeat is fresh (< 3 min old): WSL is online → Pi should be standby
#   3. If heartbeat is stale or missing: WSL is offline → Pi promotes to primary
#   4. Fallback: also check .ha_state for explicit handoff (role=primary)

export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"
STATE_FILE="$HOME/.hermes/.ha_state"
HEARTBEAT_FILE="$HOME/.hermes/.ha_heartbeat"
LOG="$HOME/.hermes/logs/ha_watchdog.log"

mkdir -p "$(dirname "$LOG")"

# --- Read heartbeat age ---
NOW=$(date +%s)
HEARTBEAT_AGE=9999  # default: very old (WSL offline)
if [ -f "$HEARTBEAT_FILE" ]; then
    HB_TS=$(cat "$HEARTBEAT_FILE" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$HB_TS" ] && [ "$HB_TS" -gt 0 ] 2>/dev/null; then
        HEARTBEAT_AGE=$(( NOW - HB_TS ))
    fi
fi

# --- Read HA state (fallback) ---
ROLE=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('role',''))" 2>/dev/null || echo "")
LAST_PRIMARY=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('last_primary',''))" 2>/dev/null || echo "")

# --- Determine if WSL is online ---
WSL_ONLINE=false
# Threshold: 180 seconds (3 minutes). WSL heartbeat runs every 2 min.
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
        echo "$(date) WSL online (heartbeat ${HEARTBEAT_AGE}s old). Stopping Pi gateway..." >> "$LOG"
        systemctl --user stop hermes-gateway.service
        # Update state to standby
        python3 -c "
import json, time
state = {'role': 'standby', 'last_sync': int(time.time()), 'last_primary': 'wsl'}
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null
        echo "$(date) Pi gateway stopped. Pi is STANDBY." >> "$LOG"
    fi
else
    # WSL is offline (heartbeat stale/missing) → check if we should promote
    if [ "$ROLE" = "primary" ] || [ "$LAST_PRIMARY" = "pi" ]; then
        # Already primary, just ensure gateway is running
        if [ "$GW_RUNNING" = "NO" ]; then
            echo "$(date) Pi is PRIMARY (no WSL heartbeat for ${HEARTBEAT_AGE}s). Starting gateway..." >> "$LOG"
            systemctl --user start hermes-gateway.service
            echo "$(date) Gateway start attempted." >> "$LOG"
        fi
    else
        # WSL was last primary but heartbeat is stale → promote Pi
        echo "$(date) WSL OFFLINE (no heartbeat for ${HEARTBEAT_AGE}s). Promoting Pi to PRIMARY..." >> "$LOG"
        # Update state
        python3 -c "
import json, time
state = {'role': 'primary', 'last_sync': int(time.time()), 'last_primary': 'pi', 'failover_reason': 'heartbeat_timeout'}
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null
        if [ "$GW_RUNNING" = "NO" ]; then
            systemctl --user start hermes-gateway.service
            echo "$(date) Pi promoted to PRIMARY. Gateway started." >> "$LOG"
        else
            echo "$(date) Pi promoted to PRIMARY. Gateway already running." >> "$LOG"
        fi
    fi
fi
WATCHDOG_EOF
chmod +x "$HOME/ha_watchdog.sh"
echo "[OK] Watchdog script deployed"

# 3. Deploy systemd service
mkdir -p "$PI_CONFIG_DIR"
cat > "$PI_CONFIG_DIR/hermes-gateway.service" << 'SERVICE_EOF'
[Unit]
Description=Hermes Agent Gateway
After=network-online.target

[Service]
Type=simple
Environment=PATH=/home/ha_user/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=/home/ha_user
ExecStart=/home/ha_user/.local/bin/hermes gateway run --replace
Restart=on-failure
RestartSec=10
WorkingDirectory=/home/ha_user

[Install]
WantedBy=default.target
SERVICE_EOF
systemctl --user daemon-reload
systemctl --user enable hermes-gateway.service
echo "[OK] Systemd service deployed and enabled"

# 4. Enable linger (user services survive logout)
loginctl enable-linger "$USER" 2>/dev/null || true
echo "[OK] Linger enabled"

# 5. Set up watchdog cron (every minute)
(contab -l 2>/dev/null | grep -v ha_watchdog; echo "* * * * * $HOME/ha_watchdog.sh") | crontab -
echo "[OK] Watchdog cron installed"

# 6. Set initial HA state to standby
python3 -c "
import json, time
state = {'role': 'standby', 'last_sync': int(time.time()), 'last_primary': 'wsl'}
with open('$PI_HERMES_HOME/.ha_state', 'w') as f:
    json.dump(state, f, indent=2)
"
echo "[OK] HA state set to standby"

echo ""
echo "=== Pi deployment complete ==="
echo "Watchdog: ~/ha_watchdog.sh (cron every minute, heartbeat-based failover)"
echo "Service:  hermes-gateway.service (enabled, controlled by watchdog)"
echo "State:    standby (waiting for WSL heartbeat timeout)"
