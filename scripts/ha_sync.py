#!/usr/bin/env python3
"""
Hermes Agent HA — Hot Standby Sync v2 (Node-Aware)
Usage:
  python3 ha_sync.py init-node       # Initialize node identity (auto-detect WSL vs Pi)
  python3 ha_sync.py status          # Show HA status with node identity
  python3 ha_sync.py takeover        # Local node takes over as primary
  python3 ha_sync.py handoff         # Local node hands off to peer
  python3 ha_sync.py push            # Periodic push to peer (backup)
  python3 ha_sync.py heartbeat       # Lightweight heartbeat to peer (cron)
  python3 ha_sync.py merge-db        # Merge SQLite databases (bidirectional)
  python3 ha_sync.py sync-version    # Check and fix version mismatch
  python3 ha_sync.py events          # Show event log (-n count, -t type)
"""

import subprocess
import sys
import os
import json
import time
import sqlite3
import argparse
import shlex
import shutil
import logging
import fcntl
import socket
import platform
from pathlib import Path
from datetime import datetime

# Add script dir for config_merge import
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

# Set up logging
logger = logging.getLogger("ha_sync")

# ============================================================
# CONFIG — adjust these for your environment
# ============================================================
PI_HOST = os.environ.get("HA_PI_HOST", "")
PI_USER = os.environ.get("HA_PI_USER", "")
if not PI_HOST or not PI_USER:
    print("Error: HA_PI_HOST and HA_PI_USER must be set (via env vars or .env)")
    sys.exit(1)
PI_SSH = f"ssh -o BatchMode=yes -o ConnectTimeout=8 {PI_USER}@{PI_HOST}"
PI_SCP = f"scp -o BatchMode=yes -o ConnectTimeout=8"

HERMES_HOME = Path.home() / ".hermes"
LOCK_FILE = HERMES_HOME / ".ha_lock"
STATE_FILE = HERMES_HOME / ".ha_state"
NODE_FILE = HERMES_HOME / ".ha_node"
PEER_FILE = HERMES_HOME / ".ha_peer"
EVENTS_FILE = HERMES_HOME / ".ha_events.jsonl"
MAX_EVENTS = 1000
HEARTBEAT_FILE_REMOTE = f"/home/{PI_USER}/.hermes/.ha_heartbeat"
# Heartbeat stale threshold in seconds. If Pi sees no heartbeat for this long,
# WSL is considered offline and Pi will promote itself to primary.
HEARTBEAT_STALE_SECONDS = int(os.environ.get("HA_HEARTBEAT_STALE", "180"))  # default 3 min

# Watermark file for incremental SQLite merge (tracks max PK per table)
WATERMARK_FILE = HERMES_HOME / ".ha_watermark.json"

# Data categories with sync strategies
# NOTE: skills/ is synced via rsync_mirror, but HA scripts (ha_sync.py, ha_watchdog.sh,
# ha_notify.sh, config_merge.py, gen_shared.py) are EXCLUDED from auto-sync.
# These files should only be updated via explicit deployment (scp/git), never by cron push.
# This prevents a stale primary from overwriting newer code on the standby.
SYNC_EXCLUDE = [
    "skills/devops/agent-ha/scripts/ha_sync.py",
    "skills/devops/agent-ha/scripts/ha_watchdog.sh",
    "skills/devops/agent-ha/scripts/ha_notify.sh",
    "skills/devops/agent-ha/scripts/config_merge.py",
    "skills/devops/agent-ha/scripts/gen_shared.py",
]

SYNC_ITEMS = {
    "sqlite_merge": [
        "memory_store.db",  # facts, entities — merge by row
        "state.db",         # sessions, messages — merge by row
    ],
    "text_latest_wins": [
        "memories/MEMORY.md",
        "memories/USER.md",
        "SOUL.md",
    ],
    "rsync_mirror": [
        "skills/",           # mirror skills tree
        "sessions/",         # session JSON files (append-only)
    ],
    "primary_only": [
        "weixin/accounts/",  # channel state (only active primary writes)
        "channel_directory.json",
        "pairing/",
    ],
}

# Tables to merge in SQLite dbs
MERGE_TABLES = {
    "state.db": {
        "sessions": ["id"],                    # PK columns
        "messages": ["id"],                    # PK columns (session_id is FK)
    },
    "memory_store.db": {
        "facts": ["fact_id"],                  # PK columns
        "entities": ["entity_id"],             # PK columns
        "fact_entities": ["fact_id", "entity_id"],  # PK columns
    },
}


# ============================================================
# LOW-LEVEL HELPERS
# ============================================================
def ssh(cmd, timeout=30):
    """Run command on Pi via SSH. Uses shlex.quote() for safe argument passing."""
    full_cmd = f"{PI_SSH} {shlex.quote(cmd)}"
    r = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.returncode


def local(cmd, timeout=30):
    """Run local command. Uses shlex.quote() for path arguments when needed."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.returncode


def local_quoted(cmd_template, *args):
    """Run local command with shlex-quoted path arguments."""
    quoted_args = [shlex.quote(str(a)) for a in args]
    full_cmd = cmd_template % tuple(quoted_args)
    r = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=30)
    return r.stdout.strip(), r.returncode


def pi_reachable():
    """Check if Pi is reachable (with retry for SSH instability)."""
    for attempt in range(3):
        try:
            out, rc = ssh("echo OK", timeout=8)
            if rc == 0 and "OK" in out:
                return True
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
            logger.debug("pi_reachable attempt %d failed: %s", attempt + 1, e)
        time.sleep(2)
    return False


# ============================================================
# NODE IDENTITY — auto-detect WSL vs Pi
# ============================================================
def detect_node():
    """Auto-detect node identity: WSL vs Raspberry Pi.
    Returns dict with: name, type, platform, arch, model, ips, version.
    """
    arch = platform.machine() or os.popen("uname -m 2>/dev/null").read().strip()

    # Check /proc/device-tree/model for Raspberry Pi
    model = ""
    node_type = "wsl"
    node_name = "wsl"
    plat = "linux"

    try:
        model_path = Path("/proc/device-tree/model")
        if model_path.exists():
            model = model_path.read_text().strip().rstrip("\x00")
            if model:
                node_type = "pi"
                node_name = "pi"
    except OSError:
        pass

    # Check if running under WSL via /proc/version
    if node_type == "wsl":
        try:
            with open("/proc/version", "r") as f:
                ver_str = f.read()
                if "microsoft" in ver_str.lower():
                    plat = "wsl"
                else:
                    plat = "linux"
        except OSError:
            plat = "linux"

    # Gather IPs
    ips = []
    try:
        hostname = socket.gethostname()
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            seen = set()
            for family, _, _, _, sockaddr in addr_info:
                ip = sockaddr[0]
                if ip not in seen and not ip.startswith("127.") and not ip.startswith("::"):
                    seen.add(ip)
                    ips.append(ip)
        except socket.gaierror:
            pass
    except Exception:
        pass

    # Get hermes version
    version = "unknown"
    try:
        out, rc = local("hermes --version 2>&1 | head -1")
        if rc == 0 and out:
            try:
                version = out.split("v")[1].split(" ")[0]
            except (IndexError, ValueError):
                version = out.strip()
    except Exception:
        pass

    return {
        "name": node_name,
        "type": node_type,
        "platform": plat,
        "arch": arch,
        "model": model,
        "ips": ips,
        "version": version,
    }


def get_node():
    """Load node identity from file, or create by detecting."""
    if NODE_FILE.exists():
        try:
            node = json.loads(NODE_FILE.read_text())
            return node
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read node file: %s", e)
    # Detect and save
    node = detect_node()
    save_node(node)
    return node


def save_node(node):
    """Write node identity dict to .ha_node JSON file."""
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    NODE_FILE.write_text(json.dumps(node, indent=2))


def discover_peer():
    """SSH to peer and gather info: arch, hostname, model, hermes_version, ha_state, gateway status.
    Returns dict or None if unreachable.
    """
    if not pi_reachable():
        return None

    peer_info = {}

    # Get peer's node identity from .ha_node file
    node_json, _ = ssh("cat ~/.hermes/.ha_node 2>/dev/null")
    if node_json:
        try:
            peer_info = json.loads(node_json)
        except json.JSONDecodeError:
            pass

    # Gather raw info if not from node file
    arch_out, _ = ssh("uname -m")
    if arch_out:
        peer_info["arch"] = arch_out

    hostname_out, _ = ssh("hostname")
    if hostname_out:
        peer_info["hostname"] = hostname_out

    model_out, _ = ssh("cat /proc/device-tree/model 2>/dev/null")
    if model_out:
        peer_info["model"] = model_out.strip().rstrip("\x00")

    ver_out, _ = ssh("export PATH=$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH; hermes --version 2>&1 | head -1")
    if ver_out:
        try:
            peer_info["hermes_version"] = ver_out.split("v")[1].split(" ")[0]
        except (IndexError, ValueError):
            peer_info["hermes_version"] = ver_out.strip()

    # HA state
    ha_state_out, _ = ssh("cat ~/.hermes/.ha_state 2>/dev/null || echo '{}'")
    if ha_state_out:
        try:
            peer_info["ha_state"] = json.loads(ha_state_out)
        except json.JSONDecodeError:
            pass

    # Gateway status
    gw_out, _ = ssh("systemctl --user is-active hermes-gateway.service 2>/dev/null")
    peer_info["gateway"] = gw_out.strip() if gw_out else "unknown"

    # Infer type if not set
    if "type" not in peer_info:
        peer_info["type"] = "pi" if peer_info.get("model") else "wsl"

    return peer_info


def get_peer():
    """Load cached peer info from .ha_peer file."""
    if PEER_FILE.exists():
        try:
            return json.loads(PEER_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read peer file: %s", e)
    return None


def save_peer(peer_info):
    """Write peer info dict to .ha_peer JSON with last_seen timestamp."""
    if peer_info is None:
        return
    peer_info["last_seen"] = time.time()
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    PEER_FILE.write_text(json.dumps(peer_info, indent=2))


def node_label():
    """Return short label like '[wsl|x86_64]' or '[pi|RPi 4 Model B Rev 1.4|aarch64]'."""
    node = get_node()
    name = node.get("name", "unknown")
    model = node.get("model", "")
    arch = node.get("arch", "")

    if name == "pi" and model:
        # Shorten "Raspberry Pi" to "RPi"
        short_model = model.replace("Raspberry Pi ", "RPi ")
        return f"[{name}|{short_model}|{arch}]"
    else:
        return f"[{name}|{arch}]"


def peer_label():
    """Return 'WSL' or 'Pi' based on local node type."""
    node = get_node()
    if node.get("type") == "pi":
        return "WSL"
    else:
        return "Pi"


# ============================================================
# EVENT LOGGING
# ============================================================
def log_event(event_type, message):
    """Append JSON line to .ha_events.jsonl with ts, time, node, node_type, event, message.
    Valid event types: failover, takeover, handoff, heartbeat_timeout, peer_online,
                       peer_offline, sync, merge, error, info.
    """
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    node = get_node()
    entry = {
        "ts": time.time(),
        "time": datetime.now().isoformat(timespec="seconds"),
        "node": node.get("name", "unknown"),
        "node_type": node.get("type", "unknown"),
        "event": event_type,
        "message": message,
    }

    # Trim old events if over MAX_EVENTS
    events = []
    if EVENTS_FILE.exists():
        try:
            with open(EVENTS_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(line)
        except OSError:
            pass
    events.append(json.dumps(entry))
    if len(events) > MAX_EVENTS:
        events = events[-MAX_EVENTS:]

    with open(EVENTS_FILE, "w") as f:
        for e in events:
            f.write(e + "\n")


def get_events(count=20, event_type=None):
    """Read last N events from .ha_events.jsonl, optional type filter.
    Returns list of dicts (newest first).
    """
    events = []
    if not EVENTS_FILE.exists():
        return events
    try:
        with open(EVENTS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        events.append(entry)
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return events

    # Filter by type if specified
    if event_type:
        events = [e for e in events if e.get("event") == event_type]

    # Return last N (newest first since file is append-only)
    return list(reversed(events[-count:]))


# ============================================================
# USER NOTIFICATION
# ============================================================
def notify_user(message):
    """Send WeChat notification via hermes chat."""
    try:
        local(f"hermes chat -q {shlex.quote(message)}", timeout=30)
    except Exception as e:
        logger.warning("notify_user failed: %s", e)


# ============================================================
# EPOCH COUNTER — prevent split-brain
# ============================================================
def get_epoch():
    """Read epoch from state file, default 0."""
    state = get_role()
    return state.get("epoch", 0)


def read_peer_epoch():
    """Read peer's current epoch and role from its state file via SSH.
    Returns dict with 'epoch', 'role', 'node', or None if unreachable.
    """
    if not pi_reachable():
        return None
    out, _ = ssh("cat ~/.hermes/.ha_state 2>/dev/null || echo '{}'")
    try:
        peer_state = json.loads(out)
        return {
            "epoch": peer_state.get("epoch", 0),
            "role": peer_state.get("role", "unknown"),
            "node": peer_state.get("node", "unknown"),
        }
    except (json.JSONDecodeError, ValueError):
        return None


def resolve_epoch(peer_epoch=None):
    """Calculate the winning epoch for a role change.
    Returns max(local_epoch, peer_epoch) + 1.
    If peer_epoch is None (unreachable), just increment local.
    This ensures the new primary always has a strictly higher epoch
    than any previous primary, preventing split-brain.
    """
    local = get_epoch()
    if peer_epoch is not None and peer_epoch > local:
        return peer_epoch + 1
    return local + 1


def check_split_brain():
    """Check if peer also thinks it's primary (potential split-brain).
    Returns: (is_conflict, peer_state_dict_or_None, description_string)
    """
    peer = read_peer_epoch()
    if peer is None:
        return False, None, "peer unreachable"

    local_state = get_role()
    local_epoch = local_state.get("epoch", 0)
    local_role = local_state.get("role", "unknown")

    if peer["role"] == "primary" and local_role == "primary":
        # Both primary — true split-brain
        if peer["epoch"] >= local_epoch:
            return True, peer, (
                f"SPLIT-BRAIN: both sides claim PRIMARY. "
                f"local epoch={local_epoch}, peer ({peer['node']}) epoch={peer['epoch']}. "
                f"Proceeding — our takeover will set epoch={peer['epoch'] + 1} to win."
            )
        else:
            # We have higher epoch — we already won previously
            return False, peer, (
                f"Peer ({peer['node']}) claims PRIMARY but epoch {peer['epoch']} < our {local_epoch}. "
                f"We already won the race. Proceeding to confirm."
            )
    elif peer["role"] == "primary":
        # Peer is primary, we are not — normal takeover scenario
        return False, peer, (
            f"Peer ({peer['node']}) is PRIMARY (epoch {peer['epoch']}). "
            f"Normal takeover — will supersede with epoch={peer['epoch'] + 1}."
        )

    return False, peer, f"Peer ({peer['node']}) role={peer['role']} epoch={peer['epoch']}"


# ============================================================
# HA STATE MANAGEMENT
# ============================================================
def get_role():
    """Read current HA role from state file. Handles new format with node/epoch fields."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read HA state file: %s", e)
    return {"role": "unknown", "last_sync": 0, "last_primary": "unknown", "node": "unknown", "node_type": "unknown", "epoch": 0}


def set_role(role, primary="unknown", epoch=None):
    """Write current HA role to state file. Includes node identity and epoch.
    epoch: if provided, use this value; otherwise auto-increment on primary/offline.
    """
    node = get_node()
    state = get_role()
    if epoch is not None:
        new_epoch = epoch
    elif role in ("primary", "offline"):
        new_epoch = state.get("epoch", 0) + 1
    else:
        new_epoch = state.get("epoch", 0)
    state = {
        "role": role,
        "last_sync": time.time(),
        "last_primary": primary,
        "node": node.get("name", "unknown"),
        "node_type": node.get("type", "unknown"),
        "epoch": new_epoch,
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))
    # Also push state to Pi
    state_pi = json.dumps({"role": "standby" if role == "primary" else "primary",
                           "last_sync": state["last_sync"],
                           "last_primary": primary,
                           "node": node.get("name", "unknown"),
                           "node_type": node.get("type", "unknown"),
                           "epoch": new_epoch})
    tmp = "/tmp/.ha_state_pi.json"
    Path(tmp).write_text(state_pi)
    _, rc = local(f"{PI_SCP} {tmp} {PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/.ha_state 2>/dev/null")
    if rc != 0:
        print(f"  [warn] Failed to push HA state to peer (rc={rc})")
    return new_epoch


def get_db_mtime(db_name, remote=False):
    """Get modification timestamp of a database."""
    path = f"~/.hermes/{db_name}"
    if remote:
        out, _ = ssh(f"stat -c %Y {path} 2>/dev/null || echo 0")
    else:
        out, _ = local(f"stat -c %Y {path} 2>/dev/null || echo 0")
    try:
        return int(out)
    except (ValueError, TypeError):
        return 0


def get_file_mtime(rel_path, remote=False):
    """Get modification timestamp of a file."""
    path = f"~/.hermes/{rel_path}"
    if remote:
        out, _ = ssh(f"stat -c %Y {path} 2>/dev/null || echo 0")
    else:
        out, _ = local(f"stat -c %Y {path} 2>/dev/null || echo 0")
    try:
        return int(out)
    except (ValueError, TypeError):
        return 0


# ============================================================
# WATERMARK: Track max PKs for incremental merge
# ============================================================
def load_watermark():
    """Load watermark state from file."""
    if WATERMARK_FILE.exists():
        try:
            return json.loads(WATERMARK_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_watermark(wm):
    """Save watermark state to file."""
    WATERMARK_FILE.write_text(json.dumps(wm, indent=2))


def reset_watermarks():
    """Reset watermarks to current max PKs (after full merge).
    Both peer_max and local_max are set to the same value since
    both sides have identical data after a full merge."""
    watermark = {}
    for db_name, tables in MERGE_TABLES.items():
        db_path = HERMES_HOME / db_name
        if not db_path.exists():
            continue
        db_wm = {}
        try:
            conn = sqlite3.connect(str(db_path))
            for table, pk_cols in tables.items():
                if len(pk_cols) == 1:
                    pk_col = pk_cols[0]
                    row = conn.execute(f"SELECT MAX({pk_col}) FROM {table}").fetchone()
                    max_pk = row[0] if row and row[0] is not None else 0
                    db_wm[table] = {"peer_max": max_pk, "local_max": max_pk}
            conn.close()
        except sqlite3.Error as e:
            logger.warning("reset_watermarks: %s error: %s", db_name, e)
        if db_wm:
            watermark[db_name] = db_wm
    save_watermark(watermark)
    logger.debug("Watermarks reset to current max PKs")


# ============================================================
# MERGE: SQLite incremental merge (watermark-based)
# ============================================================
def _row_to_json(row):
    """Convert a SQLite row to JSON-safe list (bytes → base64 string)."""
    import base64
    result = []
    for v in row:
        if isinstance(v, bytes):
            result.append({"_bytes_b64": base64.b64encode(v).decode("ascii")})
        else:
            result.append(v)
    return result


def _row_from_json(vals):
    """Convert a JSON-safe list back to original types (base64 → bytes)."""
    import base64
    result = []
    for v in vals:
        if isinstance(v, dict) and "_bytes_b64" in v:
            result.append(base64.b64decode(v["_bytes_b64"]))
        else:
            result.append(v)
    return result


def merge_sqlite_incremental(db_name):
    """Incremental merge using watermarks. Single-PK tables use max-PK
    watermarks to transfer only delta rows. Composite-PK tables fall
    back to full row exchange (small tables only)."""
    local_db = HERMES_HOME / db_name
    tables = MERGE_TABLES.get(db_name, {})
    if not tables:
        return False

    print(f"  [merge] {db_name}")

    # Get columns from local DB
    try:
        conn = sqlite3.connect(str(local_db))
    except sqlite3.Error as e:
        print(f"    Cannot open local DB: {e}")
        return False

    cols_by_table = {}
    for table in tables:
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if cols:
                cols_by_table[table] = cols
        except sqlite3.Error:
            pass
    conn.close()

    if not cols_by_table:
        print(f"    No tables found")
        return False

    watermark = load_watermark()
    db_wm = watermark.setdefault(db_name, {})

    total_new = 0

    for table, pk_cols in tables.items():
        cols = cols_by_table.get(table)
        if not cols:
            continue

        col_list = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(["?"] * len(cols))

        if len(pk_cols) == 1:
            # --- Single PK: incremental via watermark (integer PKs only) ---
            # String PKs (e.g. UUIDs) cannot use > comparison for watermarks,
            # so fall back to full row exchange.
            pk_col = pk_cols[0]

            # Check if PK is integer type by sampling a value
            pk_is_int = False
            try:
                conn = sqlite3.connect(str(local_db))
                sample = conn.execute(f"SELECT typeof({pk_col}) FROM {table} LIMIT 1").fetchone()
                conn.close()
                if sample and sample[0] in ("integer", "INTEGER"):
                    pk_is_int = True
            except sqlite3.Error:
                pass

            if not pk_is_int:
                # String PK — fall back to full exchange (same as composite PK)
                remote_full = (
                    f"import sqlite3, json, base64\n"
                    f"conn = sqlite3.connect('/home/{PI_USER}/.hermes/{db_name}')\n"
                    f"rows = conn.execute('SELECT {col_list} FROM {table}').fetchall()\n"
                    f"for row in rows:\n"
                    f"    print(json.dumps(_row_to_json(row)))\n"
                    f"conn.close()"
                )
                out, rc = ssh(f"python3 -c {shlex.quote(remote_full)}", timeout=30)

                new_from_peer = 0
                if rc == 0 and out.strip():
                    try:
                        conn = sqlite3.connect(str(local_db))
                        for line in out.strip().split("\n"):
                            try:
                                values = _row_from_json(json.loads(line))
                                conn.execute(
                                    f'INSERT OR IGNORE INTO {table} ({col_list}) '
                                    f'VALUES ({placeholders})', values
                                )
                                new_from_peer += 1
                            except (json.JSONDecodeError, sqlite3.Error):
                                pass
                        conn.commit()
                        conn.close()
                    except sqlite3.Error:
                        pass

                # Push all local rows to peer
                try:
                    conn = sqlite3.connect(str(local_db))
                    rows = conn.execute(f'SELECT {col_list} FROM {table}').fetchall()
                    conn.close()
                except sqlite3.Error:
                    rows = []

                new_to_peer = 0
                if rows:
                    safe_name = db_name.replace(".", "_")
                    delta_local = f"/tmp/ha_delta_{safe_name}_{table}.jsonl"
                    delta_remote = f"/tmp/ha_delta_{safe_name}_{table}.jsonl"
                    with open(delta_local, "w") as f:
                        for row in rows:
                            f.write(json.dumps(_row_to_json(row)) + "\n")

                    _, scp_rc = local(
                        f"{PI_SCP} {delta_local} {PI_USER}@{PI_HOST}:{delta_remote} 2>/dev/null"
                    )
                    if scp_rc == 0:
                        remote_import = (
                            f"import sqlite3, json, base64\n"
                            f"conn = sqlite3.connect('/home/{PI_USER}/.hermes/{db_name}')\n"
                            f"cols = {json.dumps(cols)}\n"
                            f"cl = ', '.join(cols)\n"
                            f"ph = ', '.join(['?'] * len(cols))\n"
                            f"count = 0\n"
                            f"with open('{delta_remote}') as f:\n"
                            f"    for line in f:\n"
                            f"        vals = json.loads(line)\n"
                            f"        vals = [base64.b64decode(v['_bytes_b64']) if isinstance(v, dict) and '_bytes_b64' in v else v for v in vals]\n"
                            f"        conn.execute('INSERT OR IGNORE INTO {table} (' + cl + ') VALUES (' + ph + ')', vals)\n"
                            f"        count += 1\n"
                            f"conn.commit()\n"
                            f"conn.close()\n"
                            f"print(count)"
                        )
                        out2, rc2 = ssh(f"python3 -c {shlex.quote(remote_import)}", timeout=30)
                        if rc2 == 0:
                            try:
                                new_to_peer = int(out2.strip())
                            except ValueError:
                                new_to_peer = len(rows)

                    Path(delta_local).unlink(missing_ok=True)
                    ssh(f"rm -f {delta_remote}")

                total_new += new_from_peer + new_to_peer
                print(f"    {table}: +{new_from_peer} from peer, +{new_to_peer} to peer (string PK, full exchange)")
                continue

            # Integer PK — use watermark for incremental
            table_wm = db_wm.setdefault(table, {"peer_max": 0, "local_max": 0})

            # (a) Pull delta from peer: rows with PK > peer_max
            peer_since = table_wm["peer_max"]
            remote_pull = (
                f"import sqlite3, json, base64\n"
                f"conn = sqlite3.connect('/home/{PI_USER}/.hermes/{db_name}')\n"
                f"rows = conn.execute('SELECT {col_list} FROM {table} "
                f"WHERE {pk_col} > {peer_since} ORDER BY {pk_col}').fetchall()\n"
                f"for row in rows:\n"
                f"    print(json.dumps(_row_to_json(row)))\n"
                f"conn.close()"
            )
            out, rc = ssh(f"python3 -c {shlex.quote(remote_pull)}", timeout=30)

            new_from_peer = 0
            max_peer_pk = peer_since
            if rc == 0 and out.strip():
                try:
                    conn = sqlite3.connect(str(local_db))
                    pk_idx = cols.index(pk_col)
                    for line in out.strip().split("\n"):
                        try:
                            values = _row_from_json(json.loads(line))
                            conn.execute(
                                f'INSERT OR IGNORE INTO {table} ({col_list}) '
                                f'VALUES ({placeholders})', values
                            )
                            new_from_peer += 1
                            if values[pk_idx] is not None and values[pk_idx] > max_peer_pk:
                                max_peer_pk = values[pk_idx]
                        except (json.JSONDecodeError, sqlite3.Error) as e:
                            logger.debug("Skip peer row: %s", e)
                    conn.commit()
                    conn.close()
                except sqlite3.Error as e:
                    logger.warning("Pull delta failed for %s.%s: %s", db_name, table, e)

            # (b) Push delta to peer: local rows with PK > local_max
            local_since = table_wm["local_max"]
            try:
                conn = sqlite3.connect(str(local_db))
                rows = conn.execute(
                    f'SELECT {col_list} FROM {table} '
                    f'WHERE {pk_col} > {local_since} ORDER BY {pk_col}'
                ).fetchall()
                conn.close()
            except sqlite3.Error:
                rows = []

            new_to_peer = 0
            max_local_pk = local_since
            if rows:
                pk_idx = cols.index(pk_col)
                # Write delta to temp file
                safe_name = db_name.replace(".", "_")
                delta_local = f"/tmp/ha_delta_{safe_name}_{table}.jsonl"
                delta_remote = f"/tmp/ha_delta_{safe_name}_{table}.jsonl"
                with open(delta_local, "w") as f:
                    for row in rows:
                        f.write(json.dumps(_row_to_json(row)) + "\n")
                        if row[pk_idx] is not None and row[pk_idx] > max_local_pk:
                            max_local_pk = row[pk_idx]

                # scp to Pi
                _, scp_rc = local(
                    f"{PI_SCP} {delta_local} {PI_USER}@{PI_HOST}:{delta_remote} 2>/dev/null"
                )
                if scp_rc == 0:
                    remote_import = (
                        f"import sqlite3, json, base64\n"
                        f"conn = sqlite3.connect('/home/{PI_USER}/.hermes/{db_name}')\n"
                        f"cols = {json.dumps(cols)}\n"
                        f"cl = ', '.join(cols)\n"
                        f"ph = ', '.join(['?'] * len(cols))\n"
                        f"count = 0\n"
                        f"with open('{delta_remote}') as f:\n"
                        f"    for line in f:\n"
f"        vals = json.loads(line)\\n"
                        f"        vals = [base64.b64decode(v['_bytes_b64']) if isinstance(v, dict) and '_bytes_b64' in v else v for v in vals]\\n"
                        f"        conn.execute('INSERT OR IGNORE INTO {table} (' + cl + ') VALUES (' + ph + ')', vals)\n"
                        f"        count += 1\n"
                        f"conn.commit()\n"
                        f"conn.close()\n"
                        f"print(count)"
                    )
                    out2, rc2 = ssh(f"python3 -c {shlex.quote(remote_import)}", timeout=30)
                    if rc2 == 0:
                        try:
                            new_to_peer = int(out2.strip())
                        except ValueError:
                            new_to_peer = len(rows)

                # Cleanup temp files
                Path(delta_local).unlink(missing_ok=True)
                ssh(f"rm -f {delta_remote}")

            # Update watermarks
            table_wm["peer_max"] = max_peer_pk
            table_wm["local_max"] = max_local_pk
            total_new += new_from_peer + new_to_peer
            print(f"    {table}: +{new_from_peer} from peer, +{new_to_peer} to peer "
                  f"(watermark: local={max_local_pk}, peer={max_peer_pk})")

        else:
            # --- Composite PK: full row exchange (small tables) ---
            remote_full = (
                f"import sqlite3, json, base64\n"
                f"conn = sqlite3.connect('/home/{PI_USER}/.hermes/{db_name}')\n"
                f"rows = conn.execute('SELECT {col_list} FROM {table}').fetchall()\n"
                f"for row in rows:\n"
                f"    print(json.dumps(_row_to_json(row)))\n"
                f"conn.close()"
            )
            out, rc = ssh(f"python3 -c {shlex.quote(remote_full)}", timeout=30)

            new_from_peer = 0
            if rc == 0 and out.strip():
                try:
                    conn = sqlite3.connect(str(local_db))
                    for line in out.strip().split("\n"):
                        try:
                            values = _row_from_json(json.loads(line))
                            conn.execute(
                                f'INSERT OR IGNORE INTO {table} ({col_list}) '
                                f'VALUES ({placeholders})', values
                            )
                            new_from_peer += 1
                        except (json.JSONDecodeError, sqlite3.Error):
                            pass
                    conn.commit()
                    conn.close()
                except sqlite3.Error:
                    pass

            # Push all local rows to peer
            try:
                conn = sqlite3.connect(str(local_db))
                rows = conn.execute(f'SELECT {col_list} FROM {table}').fetchall()
                conn.close()
            except sqlite3.Error:
                rows = []

            new_to_peer = 0
            if rows:
                safe_name = db_name.replace(".", "_")
                delta_local = f"/tmp/ha_delta_{safe_name}_{table}.jsonl"
                delta_remote = f"/tmp/ha_delta_{safe_name}_{table}.jsonl"
                with open(delta_local, "w") as f:
                    for row in rows:
                        f.write(json.dumps(_row_to_json(row)) + "\n")

                _, scp_rc = local(
                    f"{PI_SCP} {delta_local} {PI_USER}@{PI_HOST}:{delta_remote} 2>/dev/null"
                )
                if scp_rc == 0:
                    remote_import = (
                        f"import sqlite3, json, base64\n"
                        f"conn = sqlite3.connect('/home/{PI_USER}/.hermes/{db_name}')\n"
                        f"cols = {json.dumps(cols)}\n"
                        f"cl = ', '.join(cols)\n"
                        f"ph = ', '.join(['?'] * len(cols))\n"
                        f"count = 0\n"
                        f"with open('{delta_remote}') as f:\n"
                        f"    for line in f:\n"
f"        vals = json.loads(line)\\n"
                        f"        vals = [base64.b64decode(v['_bytes_b64']) if isinstance(v, dict) and '_bytes_b64' in v else v for v in vals]\\n"
                        f"        conn.execute('INSERT OR IGNORE INTO {table} (' + cl + ') VALUES (' + ph + ')', vals)\n"
                        f"        count += 1\n"
                        f"conn.commit()\n"
                        f"conn.close()\n"
                        f"print(count)"
                    )
                    out2, rc2 = ssh(f"python3 -c {shlex.quote(remote_import)}", timeout=30)
                    if rc2 == 0:
                        try:
                            new_to_peer = int(out2.strip())
                        except ValueError:
                            new_to_peer = len(rows)

                Path(delta_local).unlink(missing_ok=True)
                ssh(f"rm -f {delta_remote}")

            total_new += new_from_peer + new_to_peer
            print(f"    {table}: +{new_from_peer} from peer, +{new_to_peer} to peer (composite PK, full exchange)")

    save_watermark(watermark)
    print(f"    [incremental] {total_new} total new rows")
    return True


# ============================================================
# MERGE: SQLite full merge (legacy, used by takeover/handoff)
# ============================================================
def merge_sqlite(db_name, incremental=False):
    """Merge a SQLite database between local and Pi.
    incremental=True: use watermarks, only transfer delta rows (for cron push).
    incremental=False (default): full merge, copies entire DB (for takeover/handoff).
    After full merge, resets watermarks to current max PKs."""
    if incremental:
        return merge_sqlite_incremental(db_name)

    local_db = HERMES_HOME / db_name
    pi_db_local_copy = Path(f"/tmp/pi_{db_name}")
    merged_db = Path(f"/tmp/merged_{db_name}")

    print(f"  [merge] {db_name}")

    # 1. Pull Pi's DB
    rc = local(f"{PI_SCP} {PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/{db_name} {pi_db_local_copy} 2>/dev/null")[1]
    if rc != 0:
        print(f"    Pi DB not available, skipping merge")
        return False

    # 2. Create merged copy from local
    shutil.copy2(local_db, merged_db)

    tables = MERGE_TABLES.get(db_name, {})
    if not tables:
        print(f"    No merge tables defined for {db_name}, copying latest")
        local_mtime = get_db_mtime(db_name)
        pi_mtime = get_db_mtime(db_name, remote=True)
        if pi_mtime > local_mtime:
            shutil.copy2(pi_db_local_copy, local_db)
            print(f"    Pi DB is newer, copied to local")
        return True

    # 3. Merge each table
    src_conn = sqlite3.connect(str(pi_db_local_copy))
    src_conn.row_factory = sqlite3.Row
    dst_conn = sqlite3.connect(str(merged_db))
    dst_conn.row_factory = sqlite3.Row

    total_new = 0
    total_collisions = 0
    for table, pk_cols in tables.items():
        try:
            # Get columns
            cols = [r[1] for r in dst_conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if not cols:
                continue

            # Get existing PKs in destination
            pk_list = ", ".join(pk_cols)
            existing_pks = set()
            for row in dst_conn.execute(f"SELECT {pk_list} FROM {table}"):
                if len(pk_cols) == 1:
                    existing_pks.add(row[0])
                else:
                    existing_pks.add(tuple(row[i] for i in range(len(pk_cols))))

            # Insert rows from Pi that don't exist in local
            col_list = ", ".join(cols)
            placeholders = ", ".join(["?"] * len(cols))
            count = 0
            collisions = 0
            for row in src_conn.execute(f"SELECT {col_list} FROM {table}"):
                # Check PK
                if len(pk_cols) == 1:
                    pk_val = row[list(cols).index(pk_cols[0])]
                    pk_key = pk_val
                else:
                    pk_key = tuple(row[list(cols).index(c)] for c in pk_cols)

                if pk_key not in existing_pks:
                    values = [row[c] for c in cols]
                    dst_conn.execute(f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})", values)
                    count += 1
                else:
                    collisions += 1

            total_new += count
            total_collisions += collisions
            print(f"    {table}: +{count} new rows from Pi, {collisions} collisions (PK already exists)")

        except (sqlite3.Error, sqlite3.OperationalError) as e:
            logger.error("Table %s merge error: %s", table, e)
            print(f"    {table}: merge error: {e}")

    dst_conn.commit()
    src_conn.close()
    dst_conn.close()

    # 4. Push merged result to Pi FIRST (before replacing local)
    _, push_rc = local(f"{PI_SCP} {merged_db} {PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/{db_name} 2>/dev/null")
    if push_rc != 0:
        print(f"    WARNING: Failed to push merged DB to Pi (rc={push_rc}), NOT replacing local")
        # Cleanup temp files but keep local DB untouched
        pi_db_local_copy.unlink(missing_ok=True)
        merged_db.unlink(missing_ok=True)
        return False

    print(f"    Pushed merged DB to Pi")

    # 5. Only replace local after Pi push succeeded
    shutil.copy2(merged_db, local_db)
    print(f"    Replaced local DB ({total_new} total new rows, {total_collisions} collisions)")

    # Cleanup
    pi_db_local_copy.unlink(missing_ok=True)
    merged_db.unlink(missing_ok=True)

    # Reset watermarks to current max PKs (both sides now identical)
    reset_watermarks()

    return True


# ============================================================
# SYNC: Text files (latest mtime wins)
# ============================================================
def sync_text_latest_wins(rel_path):
    """Sync a text file: latest mtime wins. Two-way."""
    local_mtime = get_file_mtime(rel_path)
    pi_mtime = get_file_mtime(rel_path, remote=True)

    if pi_mtime == 0 and local_mtime == 0:
        return  # Neither exists

    local_path = HERMES_HOME / rel_path

    if pi_mtime > local_mtime:
        # Pi is newer -> pull
        local(f"{PI_SCP} {PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/{rel_path} {local_path} 2>/dev/null")
        print(f"  [pull] {rel_path} (Pi newer)")
    elif local_mtime > pi_mtime:
        # Local is newer -> push
        local(f"{PI_SCP} {local_path} {PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/{rel_path} 2>/dev/null")
        print(f"  [push] {rel_path} (local newer)")
    else:
        print(f"  [skip] {rel_path} (same)")


# ============================================================
# SYNC: Directory mirror (rsync)
# ============================================================
def sync_rsync_mirror(rel_path):
    """Two-way rsync: push new files from local, pull new files from Pi.
    Uses --update to skip files that are newer on the receiving side.
    Excludes HA scripts (SYNC_EXCLUDE) to prevent overwriting newer code.
    """
    dest = f"{PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/{rel_path}"
    src = str(HERMES_HOME / rel_path)

    # Build exclude args for HA scripts (only relevant for skills/ dir)
    exclude_args = ""
    for exc in SYNC_EXCLUDE:
        if exc.startswith(rel_path):
            # Relative to the rsync root
            rel_exc = exc[len(rel_path):]
            if rel_exc:
                exclude_args += f" --exclude='{rel_exc.lstrip('/')}'"

    # Push local -> Pi (add new, skip files newer on Pi)
    local(f"rsync -az --update --timeout=10{exclude_args} {src}/ {dest}/ 2>/dev/null")
    # Pull Pi -> local (add new, skip files newer locally)
    local(f"rsync -az --update --timeout=10{exclude_args} {dest}/ {src}/ 2>/dev/null")
    print(f"  [sync] {rel_path}")


# ============================================================
# SYNC: Primary-only channel state
# ============================================================
def push_primary_state():
    """Push channel state from current primary to Pi."""
    for item in SYNC_ITEMS["primary_only"]:
        src = HERMES_HOME / item
        if src.exists():
            if src.is_dir():
                local(f"rsync -az --timeout=10 {src}/ {PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/{item}/ 2>/dev/null")
            else:
                local(f"{PI_SCP} {src} {PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/{item} 2>/dev/null")
    print("  [push] channel state")


def pull_primary_state():
    """Pull channel state from Pi (Pi was primary)."""
    for item in SYNC_ITEMS["primary_only"]:
        src = f"{PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/{item}"
        dst = HERMES_HOME / item
        # Only pull if Pi's version is newer
        pi_mtime = get_file_mtime(item, remote=True)
        local_mtime = get_file_mtime(item)
        if pi_mtime > local_mtime:
            if "/" in item and not item.endswith("/"):
                # File
                local(f"{PI_SCP} {src} {dst} 2>/dev/null")
            else:
                local(f"rsync -az --timeout=10 {src}/ {dst}/ 2>/dev/null")
    print("  [pull] channel state from Pi")


# ============================================================
# GATEWAY CONTROL
# ============================================================
def stop_pi_gateway():
    """Stop gateway on Pi via systemd."""
    ssh("systemctl --user stop hermes-gateway.service 2>/dev/null", timeout=10)
    time.sleep(2)
    out, _ = ssh("systemctl --user is-active hermes-gateway.service 2>/dev/null")
    if "inactive" in out or "unknown" in out:
        print("  Pi gateway: STOPPED (systemd)")
        return True
    # Fallback: kill processes
    ssh("pkill -f 'hermes.*gateway' 2>/dev/null", timeout=5)
    time.sleep(2)
    out, _ = ssh("pgrep -f 'hermes.*gateway' || echo STOPPED", timeout=5)
    if "STOPPED" in out:
        print("  Pi gateway: STOPPED (force)")
        return True
    print("  Pi gateway: WARNING could not confirm stopped")
    return False


def start_pi_gateway():
    """Start gateway on Pi via systemd."""
    out, rc = ssh("systemctl --user start hermes-gateway.service")
    time.sleep(3)
    out2, rc2 = ssh("systemctl --user is-active hermes-gateway.service")
    if "active" in out2:
        print("  Pi gateway: STARTED (systemd)")
    else:
        # Fallback to nohup
        out, rc = ssh("export PATH=$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH; nohup hermes gateway run --replace > /dev/null 2>&1 & echo STARTED")
        print("  Pi gateway: STARTED (nohup fallback)")


def stop_local_gateway():
    """Stop local gateway and disable systemd auto-restart."""
    local("systemctl --user stop hermes-gateway.service 2>/dev/null", timeout=10)
    local("systemctl --user disable hermes-gateway.service 2>/dev/null", timeout=5)
    local("pkill -f 'hermes.*gateway' 2>/dev/null", timeout=5)
    print("  Local gateway: STOPPED (systemd disabled)")


def start_local_gateway():
    """Start local gateway and enable systemd auto-restart."""
    local("systemctl --user enable hermes-gateway.service 2>/dev/null", timeout=5)
    local("systemctl --user start hermes-gateway.service 2>/dev/null", timeout=15)
    print("  Local gateway: STARTED (systemd enabled)")


# ============================================================
# VERSION SYNC — keep both sides on same hermes version
# ============================================================
def get_hermes_version(remote=False):
    """Get hermes version string."""
    if remote:
        out, rc = ssh("export PATH=$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH; hermes --version 2>&1 | head -1")
    else:
        out, rc = local("hermes --version 2>&1 | head -1")
    # Parse "Hermes Agent v0.10.0 (2026.4.16)"
    try:
        return out.split("v")[1].split(" ")[0]
    except (IndexError, ValueError):
        return "unknown"


def sync_version_check():
    """Check if both sides have the same hermes version.
    Returns: (local_ver, pi_ver, match)
    """
    local_ver = get_hermes_version(remote=False)
    if not pi_reachable():
        return local_ver, "unreachable", True  # can't check, assume ok
    pi_ver = get_hermes_version(remote=True)
    return local_ver, pi_ver, local_ver == pi_ver


def get_config_mtime():
    """Get config.yaml mtime on both sides."""
    local_mt = get_file_mtime("config.yaml")
    pi_mt = get_file_mtime("config.yaml", remote=True)
    return local_mt, pi_mt


# ============================================================
# COMMANDS
# ============================================================
def cmd_init_node(args):
    """Initialize node identity by auto-detecting and saving."""
    node = detect_node()
    save_node(node)
    log_event("info", f"Node initialized: {node.get('name')} ({node.get('type')}) arch={node.get('arch')} model={node.get('model')}")
    print(f"Node identity detected and saved:")
    print(f"  Name:     {node.get('name')}")
    print(f"  Type:     {node.get('type')}")
    print(f"  Platform: {node.get('platform')}")
    print(f"  Arch:     {node.get('arch')}")
    print(f"  Model:    {node.get('model') or 'N/A'}")
    print(f"  IPs:      {', '.join(node.get('ips', [])) or 'N/A'}")
    print(f"  Hermes:   {node.get('version')}")
    print(f"  File:     {NODE_FILE}")


def cmd_events(args):
    """Show event log with optional filters: -n/--count (default 20), -t/--type."""
    count = args.count
    event_type = args.type

    events = get_events(count=count, event_type=event_type)

    if not events:
        if event_type:
            print(f"No events found for type '{event_type}'")
        else:
            print("No events found")
        return

    type_str = f" (type={event_type})" if event_type else ""
    print(f"HA Event Log{type_str} — last {len(events)} events:")
    print("-" * 80)
    for e in events:
        ts = e.get("time", "?")
        node = e.get("node", "?")
        ev = e.get("event", "?")
        msg = e.get("message", "")
        print(f"  {ts}  [{node}]  {ev}: {msg}")
    print("-" * 80)


def cmd_status(args):
    """Show HA status with node identity."""
    state = get_role()
    reachable = pi_reachable()
    node = get_node()

    print("=" * 50)
    print("  HERMES HA STATUS  " + node_label())
    print("=" * 50)

    # Node identity section
    print(f"  Node:       {node.get('name', 'unknown')}")
    print(f"  Hardware:   {node.get('model', 'N/A')}")
    print(f"  Arch:       {node.get('arch', 'unknown')}")
    print(f"  IPs:        {', '.join(node.get('ips', [])) or 'N/A'}")
    print(f"  Hermes:     {node.get('version', 'unknown')}")

    print(f"  Role:       {state.get('role', 'unknown')}")
    print(f"  Epoch:      {state.get('epoch', 0)}")
    print(f"  Last sync:  {time.ctime(state.get('last_sync', 0))}")
    print(f"  Last primary: {state.get('last_primary', 'unknown')}")
    print(f"  Peer:       {'YES' if reachable else 'NO'}")

    # Version check
    local_ver = get_hermes_version(remote=False)
    if reachable:
        pi_ver = get_hermes_version(remote=True)
        ver_match = "OK" if local_ver == pi_ver else "MISMATCH"
        print(f"  Version:    local={local_ver}  peer={pi_ver}  [{ver_match}]")
    else:
        print(f"  Version:    local={local_ver}")

    if reachable:
        pi_state_out, _ = ssh("cat ~/.hermes/.ha_state 2>/dev/null || echo '{}'")
        try:
            pi_state = json.loads(pi_state_out)
            print(f"  Peer HA:    {json.dumps(pi_state)}")
        except (json.JSONDecodeError, ValueError):
            print(f"  Peer HA:    (no state file)")

        # Compare key data timestamps
        print("\n  Data freshness (local vs peer):")
        for db in SYNC_ITEMS["sqlite_merge"]:
            lm = get_db_mtime(db)
            pm = get_db_mtime(db, remote=True)
            diff = "SAME" if lm == pm else ("LOCAL newer" if lm > pm else "PEER newer")
            print(f"    {db}: {diff} (local={lm}, peer={pm})")

        for f in SYNC_ITEMS["text_latest_wins"]:
            lm = get_file_mtime(f)
            pm = get_file_mtime(f, remote=True)
            diff = "SAME" if lm == pm else ("LOCAL newer" if lm > pm else "PEER newer")
            print(f"    {f}: {diff}")

    print("=" * 50)


def cmd_takeover(args):
    """Local node comes online: sync from peer, then become primary.
    Use case: WSL boots up, needs to catch up and take over.
    Epoch logic: read peer epoch, use max(local, peer)+1 to win any race.
    Split-brain check: if both sides claim PRIMARY, warn but proceed (takeover is intentional).
    """
    node = get_node()
    lbl = node_label()
    plbl = peer_label()

    print("=" * 50)
    print(f"  HA TAKEOVER: {lbl} initiating takeover")
    print("=" * 50)

    # --- Epoch: check peer state for split-brain ---
    is_conflict, peer_info, desc = check_split_brain()
    peer_epoch = peer_info["epoch"] if peer_info else None
    if peer_info:
        print(f"  Peer state: {desc}")
    if is_conflict:
        log_event("error", f"Takeover with conflict: {desc}")
        print(f"  ⚠️  SPLIT-BRAIN DETECTED — proceeding with takeover (epoch will supersede)")

    # Calculate winning epoch BEFORE any state change
    winning_epoch = resolve_epoch(peer_epoch)
    print(f"  Epoch: local={get_epoch()}, peer={peer_epoch}, winning={winning_epoch}")

    if not pi_reachable():
        print(f"  {plbl} not reachable, assuming standalone mode")
        set_role("primary", node.get("name", "wsl"), epoch=winning_epoch)
        log_event("takeover", f"{lbl} became primary (standalone — {plbl} unreachable, epoch {winning_epoch})")
        notify_user(f"\U0001f514 HA: {lbl} is now PRIMARY (standalone, epoch {winning_epoch})")
        return

    # 1. Pull and merge peer's data (peer was primary while we were offline)
    print("\n[1/5] Merging databases...")
    for db in SYNC_ITEMS["sqlite_merge"]:
        merge_sqlite(db)

    print("\n[2/5] Syncing text files...")
    for f in SYNC_ITEMS["text_latest_wins"]:
        sync_text_latest_wins(f)

    print("\n[3/5] Syncing directories...")
    for d in SYNC_ITEMS["rsync_mirror"]:
        sync_rsync_mirror(d)

    # Sync shared config (pull from peer, merge locally)
    shared_cfg = HERMES_HOME / "config.shared.yaml"
    pi_shared = f"/home/{PI_USER}/.hermes/config.shared.yaml"
    pi_mt = get_file_mtime("config.shared.yaml", remote=True)
    local_mt = int(shared_cfg.stat().st_mtime) if shared_cfg.exists() else 0
    if pi_mt > local_mt:
        local(f"{PI_SCP} {PI_USER}@{PI_HOST}:{pi_shared} {shared_cfg} 2>/dev/null")
        print("  [pull] config.shared.yaml (peer newer)")
    # Always merge shared config into local config.yaml
    try:
        from config_merge import merge_config
        merge_config()
        print("  [merge] config.shared.yaml -> local config.yaml")
    except (ImportError, OSError) as e:
        print(f"  [warn] config merge failed: {e}")

    print("\n[4/5] Pulling channel state from peer...")
    pull_primary_state()

    # 2. Stop peer's gateway
    print("\n[5/5] Stopping peer gateway...")
    stop_pi_gateway()

    # 3. Ensure local gateway is running
    print("\nEnsuring local gateway is running...")
    start_local_gateway()

    # Clear stale heartbeat on peer (we are back online, will write fresh ones)
    ssh(f"rm -f {HEARTBEAT_FILE_REMOTE}")

    # 4. Record state with winning epoch
    node_name = node.get("name", "wsl")
    set_role("primary", node_name, epoch=winning_epoch)
    log_event("takeover", f"{lbl} is now PRIMARY (epoch {winning_epoch})")
    notify_user(f"\U0001f514 HA: {lbl} is now PRIMARY (epoch {winning_epoch})")
    print(f"\n  {lbl} is now PRIMARY (epoch {winning_epoch}). {plbl} is STANDBY.")
    print("  Gateway running locally. All channels active.")


def cmd_handoff(args):
    """Local node going offline: push data to peer, let peer take over.
    Use case: shutting down, planned maintenance.
    """
    node = get_node()
    lbl = node_label()
    plbl = peer_label()

    print("=" * 50)
    print(f"  HA HANDOFF: {lbl} handing off to {plbl}")
    print("=" * 50)

    if not pi_reachable():
        print(f"  WARNING: {plbl} not reachable! Cannot hand off.")
        print("  Data will only be available locally.")
        log_event("error", f"Handoff FAILED: {plbl} unreachable")
        return

    # Read peer epoch for resolve_epoch later
    peer_ep_result = read_peer_epoch()
    peer_ep = peer_ep_result["epoch"] if peer_ep_result else None

    # 1. Push all data to peer
    print("\n[1/4] Merging databases...")
    for db in SYNC_ITEMS["sqlite_merge"]:
        merge_sqlite(db)

    print("\n[2/4] Syncing text files & directories...")
    for f in SYNC_ITEMS["text_latest_wins"]:
        sync_text_latest_wins(f)
    for d in SYNC_ITEMS["rsync_mirror"]:
        sync_rsync_mirror(d)

    # Sync shared config to peer and merge
    shared_cfg = HERMES_HOME / "config.shared.yaml"
    if shared_cfg.exists():
        local(f"{PI_SCP} {shared_cfg} {PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/config.shared.yaml 2>/dev/null")
        local(f"{PI_SCP} {SCRIPT_DIR}/config_merge.py {PI_USER}@{PI_HOST}:/tmp/config_merge.py 2>/dev/null")
        ssh("python3 /tmp/config_merge.py 2>&1", timeout=10)
        print("  [sync] config.shared.yaml -> peer")

    print("\n[3/4] Pushing channel state...")
    push_primary_state()

    # 2. Stop local gateway
    print("\n[4/4] Stopping local gateway...")
    stop_local_gateway()

    # 3. Start peer gateway
    print("\nStarting peer gateway...")
    start_pi_gateway()

    # 4. Record state with resolved epoch
    handoff_epoch = resolve_epoch(peer_ep)
    set_role("offline", plbl.lower(), epoch=handoff_epoch)
    log_event("handoff", f"{lbl} handed off to {plbl} (epoch {handoff_epoch})")
    notify_user(f"\U0001f514 HA: {lbl} handed off, {plbl} is now PRIMARY (epoch {handoff_epoch})")
    print(f"\n  {plbl} is now PRIMARY (epoch {handoff_epoch}). {lbl} is OFFLINE.")
    print(f"  Gateway running on {plbl}.")


def cmd_push(args):
    """Periodic push: sync local data to/from peer while local is primary.
    Use case: cron job every 30 min while local is online.
    SQLite DBs use bidirectional merge (INSERT OR IGNORE) to preserve any
    rows written on the peer (e.g. Holographic memory hooks on standby).
    """
    state = get_role()
    if state.get("role") != "primary":
        print("Not primary, skipping push")
        return

    lbl = node_label()
    plbl = peer_label()
    print(f"[{time.strftime('%H:%M:%S')}] Periodic sync to {plbl}...")

    if not pi_reachable():
        print(f"  {plbl} not reachable, skipping")
        log_event("error", f"Push skipped: {plbl} unreachable")
        return

    # Merge SQLite DBs bidirectionally (incremental via watermarks)
    for db in SYNC_ITEMS["sqlite_merge"]:
        if not merge_sqlite(db, incremental=True):
            log_event("error", f"Merge failed: {db}")

    for f in SYNC_ITEMS["text_latest_wins"]:
        local_path = HERMES_HOME / f
        if local_path.exists():
            local(f"{PI_SCP} {local_path} {PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/{f} 2>/dev/null")

    for d in SYNC_ITEMS["rsync_mirror"]:
        src = HERMES_HOME / d
        if src.exists():
            sync_rsync_mirror(d)  # uses SYNC_EXCLUDE internally

    push_primary_state()

    # Sync shared config
    shared_cfg = HERMES_HOME / "config.shared.yaml"
    if shared_cfg.exists():
        local(f"{PI_SCP} {shared_cfg} {PI_USER}@{PI_HOST}:/home/{PI_USER}/.hermes/config.shared.yaml 2>/dev/null")
        local(f"{PI_SCP} {SCRIPT_DIR}/config_merge.py {PI_USER}@{PI_HOST}:/tmp/config_merge.py 2>/dev/null")
        ssh("python3 /tmp/config_merge.py 2>&1", timeout=10)
        print("  [sync] config.shared.yaml -> peer")
    else:
        print("  [skip] config.shared.yaml not found")

    state["last_sync"] = time.time()
    STATE_FILE.write_text(json.dumps(state, indent=2))
    # Also write heartbeat (push already has SSH connection)
    write_heartbeat()
    log_event("sync", f"Periodic push to {plbl} completed")
    print(f"  Done")


def write_heartbeat():
    """Write current timestamp to Pi's heartbeat file. Lightweight — just one SSH call."""
    out, rc = ssh(f"echo {int(time.time())} > {HEARTBEAT_FILE_REMOTE}")
    return rc == 0


def cmd_heartbeat(args):
    """Lightweight heartbeat: write timestamp to peer to signal local is alive.
    Called by cron every 2 minutes. Peer watchdog checks this file to detect offline.
    """
    if write_heartbeat():
        print(f"[{time.strftime('%H:%M:%S')}] Heartbeat written to peer")
    else:
        print(f"[{time.strftime('%H:%M:%S')}] Heartbeat FAILED (peer unreachable?)")
        log_event("heartbeat_timeout", "Heartbeat write failed — peer unreachable")


def cmd_merge_db(args):
    """Manually merge a specific database."""
    db = args.db
    if db not in MERGE_TABLES:
        print(f"Unknown DB: {db}. Valid: {list(MERGE_TABLES.keys())}")
        return
    merge_sqlite(db)


def cmd_sync_version(args):
    """Check version mismatch and update the older side.
    Strategy: local is primary — if versions differ, update peer to match local.
    If local is older, update local first, then tell user to update peer too.
    """
    print("=" * 50)
    print("  VERSION SYNC CHECK")
    print("=" * 50)

    local_ver = get_hermes_version(remote=False)
    print(f"  Local version: {local_ver}")

    if not pi_reachable():
        print("  Peer not reachable, cannot sync version")
        return

    pi_ver = get_hermes_version(remote=True)
    print(f"  Peer version:  {pi_ver}")

    if local_ver == pi_ver:
        print("\n  Versions match. No action needed.")
        return

    # Determine which is newer by parsing semver
    def parse_ver(v):
        parts = v.split(".")
        return tuple(int(p) for p in parts)

    try:
        lv = parse_ver(local_ver)
        pv = parse_ver(pi_ver)
    except (ValueError, IndexError):
        print(f"\n  Cannot compare versions. Update manually.")
        return

    if lv < pv:
        # Peer is newer — update local
        print(f"\n  Peer is newer ({pi_ver} > {local_ver}). Updating local...")
        out, rc = local("hermes update 2>&1", timeout=300)
        print(out[-500:] if len(out) > 500 else out)
        new_ver = get_hermes_version(remote=False)
        print(f"  Local now: {new_ver}")
    elif lv > pv:
        # Local is newer — update peer
        print(f"\n  Local is newer ({local_ver} > {pi_ver}). Updating peer...")
        out, rc = ssh("export PATH=$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH; hermes update 2>&1", timeout=300)
        print(out[-500:] if len(out) > 500 else out)
        new_pi_ver = get_hermes_version(remote=True)
        print(f"  Peer now: {new_pi_ver}")

        # Also migrate peer config if needed
        ssh("export PATH=$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH; hermes config migrate 2>&1", timeout=30)
        print("  Peer config migrated")
    else:
        print("  Versions match.")

    # Final check
    final_local = get_hermes_version(remote=False)
    final_pi = get_hermes_version(remote=True)
    match = "OK" if final_local == final_pi else "STILL MISMATCH"
    print(f"\n  Final: local={final_local} peer={final_pi} [{match}]")


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Hermes HA Sync v2 — Node-Aware Hot Standby")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init-node", help="Initialize node identity (auto-detect WSL vs Pi)")
    sub.add_parser("status", help="Show HA status with node identity")
    sub.add_parser("takeover", help="Local node takes over as primary")
    sub.add_parser("handoff", help="Local node hands off to peer")
    sub.add_parser("push", help="Periodic push to peer (cron)")
    sub.add_parser("heartbeat", help="Lightweight heartbeat to peer (cron)")
    sub.add_parser("sync-version", help="Check and fix version mismatch")
    merge_p = sub.add_parser("merge-db", help="Merge a specific database")
    merge_p.add_argument("db", help="Database name (state.db or memory_store.db)")

    events_p = sub.add_parser("events", help="Show event log")
    events_p.add_argument("-n", "--count", type=int, default=20, help="Number of events to show (default: 20)")
    events_p.add_argument("-t", "--type", default=None, help="Filter by event type")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # C-4: Acquire file lock for all commands
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
    except (OSError, BlockingIOError):
        print("Another ha_sync.py instance is running (lock held). Exiting.")
        sys.exit(1)

    try:
        if args.command == "init-node":
            cmd_init_node(args)
        elif args.command == "status":
            cmd_status(args)
        elif args.command == "takeover":
            cmd_takeover(args)
        elif args.command == "handoff":
            cmd_handoff(args)
        elif args.command == "push":
            cmd_push(args)
        elif args.command == "heartbeat":
            cmd_heartbeat(args)
        elif args.command == "merge-db":
            cmd_merge_db(args)
        elif args.command == "sync-version":
            cmd_sync_version(args)
        elif args.command == "events":
            cmd_events(args)
        else:
            parser.print_help()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
