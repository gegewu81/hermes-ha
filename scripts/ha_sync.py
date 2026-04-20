#!/usr/bin/env python3
"""
Hermes Agent HA v3 — File-First Sync (ha_sync.py)

Sync source files (sessions JSONL, memory DB→JSON, config, SOUL, skills),
rebuild state.db locally. Never merge DBs — DBs are disposable, files are truth.

Usage:
    ha_sync.py init-node [--role primary|standby]
    ha_sync.py status
    ha_sync.py push [--dry-run]
    ha_sync.py rebuild [--all]
    ha_sync.py takeover
    ha_sync.py handoff
    ha_sync.py heartbeat
    ha_sync.py idle-push [--idle-minutes 10]
    ha_sync.py sync-version
    ha_sync.py events [--last N]
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────

HERMES_DIR = Path(os.environ.get("HERMES_DIR", "~/.hermes")).expanduser()
SCRIPT_DIR = Path(__file__).resolve().parent
PI_HOST = "pi"
HERMES_USER = "chao"  # local username (same on Pi)

# Key paths
SESSIONS_DIR = HERMES_DIR / "sessions"
STATE_DB = HERMES_DIR / "state.db"
MEMORY_DB = HERMES_DIR / "memory_store.db"
CONFIG_YAML = HERMES_DIR / "config.yaml"
SOUL_MD = HERMES_DIR / "SOUL.md"
MEMORY_JSON = HERMES_DIR / "memory.json"
SKILLS_DIR = HERMES_DIR / "skills"

# HA state files
HA_STATE_DIR = HERMES_DIR / ".ha"
HA_NODE_FILE = HA_STATE_DIR / "node.json"
HA_HEARTBEAT_FILE = HA_STATE_DIR / "heartbeat"
HA_EVENTS_LOG = HA_STATE_DIR / "events.log"
HA_EPOCH_FILE = HA_STATE_DIR / "epoch"

# Timeouts (seconds)
SSH_TIMEOUT = 10
RSYNC_TIMEOUT = 120

# ── Helpers ────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    if level != "DEBUG":
        print(line, file=sys.stderr)
    append_event(msg, level)


def append_event(msg: str, level: str = "INFO"):
    """Append to HA events log (rotated at 5000 lines). Only INFO+."""
    if level == "DEBUG":
        return
    HA_STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(HA_EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{level}] {msg}\n")
        # Rotate if too large
        try:
            with open(HA_EVENTS_LOG, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > 5000:
                with open(HA_EVENTS_LOG, "w", encoding="utf-8") as f:
                    f.writelines(lines[-3000:])
        except Exception:
            pass
    except Exception:
        pass


def load_node_state() -> dict:
    """Load local node state."""
    if not HA_NODE_FILE.exists():
        return {}
    try:
        with open(HA_NODE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_node_state(state: dict):
    """Save local node state."""
    HA_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(HA_NODE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def run(cmd: str, check: bool = True, timeout: int = None, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command."""
    log(f"CMD: {cmd}", "DEBUG")
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=capture, text=True,
            timeout=timeout, check=False
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed (rc={result.returncode}): {cmd}\nstderr: {result.stderr[:500]}")
        return result
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout}s: {cmd}")


def ssh_pi(cmd: str, timeout: int = SSH_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a command on Pi via SSH."""
    full_cmd = f"ssh -o ConnectTimeout={SSH_TIMEOUT} -o BatchMode=yes {PI_HOST} '{cmd}'"
    return run(full_cmd, timeout=timeout)


def ssh_pi_hermes_path() -> str:
    """Detect Hermes home on Pi (usually same path)."""
    r = ssh_pi("echo $HOME", timeout=SSH_TIMEOUT)
    pi_home = r.stdout.strip()
    return f"{pi_home}/.hermes"


def get_epoch() -> int:
    """Get current epoch counter."""
    if HA_EPOCH_FILE.exists():
        try:
            return int(HA_EPOCH_FILE.read_text().strip())
        except (ValueError, OSError):
            pass
    return 0


def set_epoch(val: int):
    HA_STATE_DIR.mkdir(parents=True, exist_ok=True)
    HA_EPOCH_FILE.write_text(str(val))


def increment_epoch() -> int:
    val = get_epoch() + 1
    set_epoch(val)
    return val


def get_remote_epoch() -> int:
    """Get epoch from Pi."""
    try:
        r = ssh_pi("cat ~/.hermes/.ha/epoch 2>/dev/null || echo 0", timeout=SSH_TIMEOUT)
        return int(r.stdout.strip())
    except Exception:
        return 0


def pi_reachable() -> bool:
    """Check if Pi is reachable via SSH."""
    try:
        r = ssh_pi("echo ok", timeout=5)
        return r.returncode == 0 and "ok" in r.stdout
    except Exception:
        return False


def gateway_running() -> bool:
    """Check if Hermes gateway is running locally."""
    pid_file = HERMES_DIR / "gateway.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # check if process exists
            return True
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    return False


def gateway_running_on_pi() -> bool:
    """Check if gateway is running on Pi."""
    try:
        r = ssh_pi(f"test -f {ssh_pi_hermes_path()}/gateway.pid && "
                   f"kill -0 $(cat {ssh_pi_hermes_path()}/gateway.pid) 2>/dev/null && echo running || echo stopped",
                   timeout=SSH_TIMEOUT)
        return "running" in r.stdout.strip()
    except Exception:
        return False


def hermes_version() -> str:
    """Get local Hermes version."""
    try:
        r = run("hermes version 2>/dev/null || hermes --version 2>/dev/null", check=False)
        return r.stdout.strip().split("\n")[0]
    except Exception:
        return "unknown"


def hermes_version_pi() -> str:
    """Get Pi Hermes version."""
    try:
        r = ssh_pi("hermes version 2>/dev/null || hermes --version 2>/dev/null", timeout=SSH_TIMEOUT)
        return r.stdout.strip().split("\n")[0]
    except Exception:
        return "unknown"


# ── State DB Schema ────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT '',
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_version (version) VALUES (5);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

# ── Core Commands ──────────────────────────────────────────────

def cmd_init_node(args):
    """Initialize this node's HA identity."""
    role = args.role or "standby"
    if role not in ("primary", "standby"):
        print("Error: role must be 'primary' or 'standby'", file=sys.stderr)
        sys.exit(1)

    state = load_node_state()
    if state.get("role"):
        print(f"Node already initialized as '{state['role']}' at {state.get('initialized_at')}")
        force = getattr(args, "force", False)
        if not force and sys.stdin.isatty():
            if input(f"Re-initialize as '{role}'? [y/N] ").strip().lower() != "y":
                return
        elif not force:
            print("  (skipped: non-interactive mode, use --force to re-init)")
            return

    state = {
        "role": role,
        "initialized_at": datetime.now(timezone.utc).isoformat(),
        "hostname": os.uname().nodename,
    }
    save_node_state(state)
    set_epoch(1)
    print(f"Node initialized: role={role}, hostname={state['hostname']}")


def cmd_status(args):
    """Show HA status."""
    state = load_node_state()
    role = state.get("role", "uninitialized")

    print(f"=== Hermes HA Status ===")
    print(f"  Node role:     {role}")
    print(f"  Hostname:      {state.get('hostname', '?')}")
    print(f"  Initialized:   {state.get('initialized_at', '?')}")
    print(f"  Epoch:         {get_epoch()}")

    # Gateway status
    gw = gateway_running()
    print(f"  Gateway:       {'RUNNING' if gw else 'STOPPED'}")

    # File counts
    sessions = list(SESSIONS_DIR.glob("*.jsonl"))
    print(f"  Sessions:      {len(sessions)} JSONL files")
    if STATE_DB.exists():
        size_mb = STATE_DB.stat().st_size / (1024 * 1024)
        print(f"  state.db:      {size_mb:.1f} MB")
    else:
        print(f"  state.db:      MISSING")
    if MEMORY_DB.exists():
        size_kb = MEMORY_DB.stat().st_size / 1024
        print(f"  memory_store:  {size_kb:.1f} KB")
    else:
        print(f"  memory_store:  MISSING")

    # Pi status
    print(f"\n  --- Pi Node ---")
    if pi_reachable():
        print(f"  SSH:           REACHABLE")
        print(f"  Hermes ver:    {hermes_version_pi()}")
        pi_gw = gateway_running_on_pi()
        print(f"  Gateway:       {'RUNNING' if pi_gw else 'STOPPED'}")
        pi_hermes = ssh_pi_hermes_path()
        r = ssh_pi(f"ls {pi_hermes}/sessions/*.jsonl 2>/dev/null | wc -l", timeout=SSH_TIMEOUT)
        print(f"  Sessions:      {r.stdout.strip()} JSONL files")
        print(f"  Remote epoch:  {get_remote_epoch()}")
    else:
        print(f"  SSH:           UNREACHABLE")

    print(f"\n  --- Local ---")
    print(f"  Hermes ver:    {hermes_version()}")


def cmd_push(args):
    """Full sync: files → Pi, trigger remote rebuild."""
    if not pi_reachable():
        log("Pi unreachable, skipping push")
        print("ERROR: Pi not reachable via SSH")
        sys.exit(1)

    state = load_node_state()
    if state.get("role") != "primary":
        log("Push attempted from non-primary node", "WARN")
        print("WARNING: Not running as primary. Proceeding anyway.")

    epoch = increment_epoch()
    pi_hermes = ssh_pi_hermes_path()
    dry_run = getattr(args, "dry_run", False)
    dry_flag = "--dry-run" if dry_run else ""

    print(f"[1/6] Pushing sessions (rsync)...")
    run(f"rsync -avz --update --timeout={RSYNC_TIMEOUT} {dry_flag} "
        f"{SESSIONS_DIR}/*.jsonl {PI_HOST}:{pi_hermes}/sessions/",
        check=False, timeout=RSYNC_TIMEOUT + 30)

    print(f"[2/6] Exporting memory_store.db → JSON...")
    mem_json = export_memory_db()
    if mem_json:
        run(f"scp -o ConnectTimeout={SSH_TIMEOUT} {mem_json} "
            f"{PI_HOST}:{pi_hermes}/memory_export.json",
            check=False, timeout=RSYNC_TIMEOUT)
        print(f"  Exported {mem_json}")
        # Clean up local temp file
        try:
            os.unlink(mem_json)
        except OSError:
            pass

    print(f"[3/6] Pushing config.yaml...")
    run(f"scp -o ConnectTimeout={SSH_TIMEOUT} {CONFIG_YAML} "
        f"{PI_HOST}:{pi_hermes}/config.yaml",
        check=False, timeout=RSYNC_TIMEOUT)

    if SOUL_MD.exists():
        print(f"[3.5/6] Pushing SOUL.md...")
        run(f"scp -o ConnectTimeout={SSH_TIMEOUT} {SOUL_MD} "
            f"{PI_HOST}:{pi_hermes}/SOUL.md",
            check=False, timeout=RSYNC_TIMEOUT)

    if MEMORY_JSON.exists():
        print(f"[3.6/6] Pushing memory.json...")
        run(f"scp -o ConnectTimeout={SSH_TIMEOUT} {MEMORY_JSON} "
            f"{PI_HOST}:{pi_hermes}/memory.json",
            check=False, timeout=RSYNC_TIMEOUT)

    print(f"[4/6] Pushing skills (rsync)...")
    run(f"rsync -avz --delete --timeout={RSYNC_TIMEOUT} {dry_flag} "
        f"{SKILLS_DIR}/ {PI_HOST}:{pi_hermes}/skills/",
        check=False, timeout=RSYNC_TIMEOUT + 60)

    print(f"[5/6] Sending epoch ({epoch})...")
    ssh_pi(f"mkdir -p {pi_hermes}/.ha && echo {epoch} > {pi_hermes}/.ha/epoch",
           timeout=SSH_TIMEOUT)

    if not dry_run:
        print(f"[6/6] Triggering remote rebuild...")
        # SCP the rebuild script to Pi and execute it
        rebuild_script = SCRIPT_DIR / "ha_rebuild_remote.py"
        remote_tmp = "/tmp/ha_rebuild_remote.py"
        run(f"scp -o ConnectTimeout={SSH_TIMEOUT} {rebuild_script} "
            f"{PI_HOST}:{remote_tmp}",
            check=False, timeout=RSYNC_TIMEOUT)
        r = ssh_pi(f"python3 {remote_tmp} --hermes-dir {pi_hermes}",
                   timeout=300)
        if r.returncode == 0:
            print(f"  Remote rebuild:\n{r.stdout}")
        else:
            log(f"Remote rebuild failed: {r.stderr}", "ERROR")
            print(f"  WARNING: Remote rebuild failed: {r.stderr[:300]}")
        # Clean up
        ssh_pi(f"rm -f {remote_tmp}", timeout=SSH_TIMEOUT)

    log(f"Push complete (epoch={epoch})")
    print(f"\nPush complete. Epoch: {epoch}")


def export_memory_db() -> str:
    """Export memory_store.db to a temporary JSON file."""
    if not MEMORY_DB.exists():
        return None

    export_path = HERMES_DIR / "memory_export.json"
    try:
        conn = sqlite3.connect(str(MEMORY_DB))
        conn.row_factory = sqlite3.Row

        facts = []
        for row in conn.execute("SELECT * FROM facts"):
            facts.append(dict(row))

        entities = []
        for row in conn.execute("SELECT * FROM entities"):
            entities.append(dict(row))

        conn.close()

        data = {"exported_at": datetime.now(timezone.utc).isoformat(), "facts": facts, "entities": entities}
        with open(export_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        return str(export_path)
    except Exception as e:
        log(f"Memory export failed: {e}", "ERROR")
        return None


def cmd_rebuild(args):
    """Rebuild local state.db from sessions JSONL files."""
    do_all = getattr(args, "all", False)

    if do_all or True:  # Always rebuild state.db
        print("Rebuilding state.db from sessions/...")
        rebuild_state_db()
        print("Done.")

    if do_all:
        print("\nRebuilding memory_store.db is not supported locally.")
        print("Memory DB is managed by the holographic memory plugin.")
        print("Use 'push' to sync memory to Pi, which will import the export.")


def rebuild_state_db():
    """Rebuild state.db from all JSONL session files."""
    if not SESSIONS_DIR.exists():
        print("ERROR: sessions directory not found")
        return

    jsonl_files = sorted(SESSIONS_DIR.glob("*.jsonl"))
    if not jsonl_files:
        print("No JSONL session files found")
        return

    # Backup existing DB
    if STATE_DB.exists():
        backup = STATE_DB.with_suffix(f".db.bak.{int(time.time())}")
        shutil.copy2(STATE_DB, backup)
        print(f"Backed up to {backup.name}")

    # Remove old DB
    STATE_DB.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        p = STATE_DB.parent / (STATE_DB.name + suffix)
        p.unlink(missing_ok=True)

    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)

    msg_id = 0
    session_count = 0
    msg_count = 0

    for jf in jsonl_files:
        sid = jf.stem  # e.g. 20260412_124237_ae123112

        # Skip if already in DB (from previous partial rebuild)
        existing = conn.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone()
        if existing:
            continue

        # Parse started_at from filename: YYYYMMDD_HHMMSS_random
        try:
            parts = sid.split("_")
            dt_str = f"{parts[0]}_{parts[1]}"
            dt = datetime.strptime(dt_str, "%Y%m%d_%H%M%S")
            started_at = dt.timestamp()
        except (IndexError, ValueError):
            started_at = jf.stat().st_mtime

        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            (sid, "cli", started_at)
        )

        with open(jf, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_id += 1
                role = obj.get("role", "user")
                content = obj.get("content", "")

                # Tool calls as JSON string
                tool_calls = obj.get("tool_calls")
                tool_calls_str = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
                tool_name = obj.get("tool_name")

                # Timestamp
                ts = obj.get("timestamp", started_at)
                if isinstance(ts, str):
                    try:
                        ts = float(ts)
                    except (ValueError, TypeError):
                        ts = started_at
                elif not isinstance(ts, (int, float)):
                    ts = started_at

                conn.execute(
                    "INSERT INTO messages (id, session_id, role, content, tool_calls, tool_name, timestamp) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (msg_id, sid, role, content, tool_calls_str, tool_name, ts)
                )
                msg_count += 1

        session_count += 1
        # Update session message count
        session_msgs = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE sessions SET message_count=? WHERE id=?", (session_msgs, sid)
        )

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # Stats
    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()

    print(f"Rebuilt state.db: {session_count} new sessions, {msg_count} new messages")
    print(f"Total: {total_sessions} sessions, {total_messages} messages")


def cmd_takeover(args):
    """Pull Pi data, rebuild local, become primary."""
    if not pi_reachable():
        print("ERROR: Pi not reachable")
        sys.exit(1)

    state = load_node_state()
    if state.get("role") == "primary":
        print("Already primary. Use handoff first if switching.")
        sys.exit(1)

    pi_epoch = get_remote_epoch()
    local_epoch = get_epoch()
    if pi_epoch < local_epoch:
        print(f"WARNING: Local epoch ({local_epoch}) > Pi epoch ({pi_epoch}). Possible split-brain.")
        if input("Proceed anyway? [y/N] ").strip().lower() != "y":
            return

    pi_hermes = ssh_pi_hermes_path()

    print("[1/4] Pulling sessions from Pi...")
    run(f"rsync -avz --update --timeout={RSYNC_TIMEOUT} "
        f"{PI_HOST}:{pi_hermes}/sessions/*.jsonl {SESSIONS_DIR}/",
        check=False, timeout=RSYNC_TIMEOUT + 30)

    print("[2/4] Pulling config from Pi...")
    run(f"scp -o ConnectTimeout={SSH_TIMEOUT} "
        f"{PI_HOST}:{pi_hermes}/config.yaml {CONFIG_YAML}",
        check=False, timeout=RSYNC_TIMEOUT)

    for f_name in ["SOUL.md", "memory.json"]:
        r = ssh_pi(f"test -f {pi_hermes}/{f_name} && echo exists || echo missing", timeout=SSH_TIMEOUT)
        if "exists" in r.stdout:
            run(f"scp -o ConnectTimeout={SSH_TIMEOUT} "
                f"{PI_HOST}:{pi_hermes}/{f_name} {HERMES_DIR}/{f_name}",
                check=False, timeout=RSYNC_TIMEOUT)

    print("[3/4] Rebuilding local state.db...")
    rebuild_state_db()

    print("[4/4] Promoting to primary...")
    epoch = max(local_epoch, pi_epoch) + 1
    set_epoch(epoch)
    state["role"] = "primary"
    save_node_state(state)

    log(f"Takeover complete (epoch={epoch})")
    print(f"\nTakeover complete. This node is now PRIMARY (epoch={epoch})")
    print("Start gateway with: hermes gateway")


def cmd_handoff(args):
    """Push data to Pi, stop local gateway, demote to standby."""
    state = load_node_state()
    if state.get("role") != "primary":
        print("Not primary. Nothing to hand off.")
        sys.exit(1)

    print("[1/3] Pushing all data to Pi...")
    # Reuse push logic
    args.dry_run = False
    cmd_push(args)

    print("[2/3] Stopping local gateway...")
    if gateway_running():
        pid_file = HERMES_DIR / "gateway.pid"
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)  # SIGTERM
            time.sleep(2)
            os.kill(pid, 0)  # Check if still running
            os.kill(pid, 9)  # SIGKILL
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        print("  Gateway stopped.")
    else:
        print("  Gateway was not running.")

    print("[3/3] Demoting to standby...")
    epoch = increment_epoch()
    state["role"] = "standby"
    state["demoted_at"] = datetime.now(timezone.utc).isoformat()
    save_node_state(state)

    log(f"Handoff complete (epoch={epoch})")
    print(f"\nHandoff complete. Pi is now PRIMARY (epoch={epoch})")


def cmd_heartbeat(args):
    """Write heartbeat timestamp to Pi."""
    if not pi_reachable():
        log("Heartbeat failed: Pi unreachable", "WARN")
        return

    now = datetime.now(timezone.utc).isoformat()
    ssh_pi(f"mkdir -p {ssh_pi_hermes_path()}/.ha && echo '{now}' > {ssh_pi_hermes_path()}/.ha/heartbeat_primary",
           timeout=SSH_TIMEOUT)
    # Also write local heartbeat
    HA_STATE_DIR.mkdir(parents=True, exist_ok=True)
    HA_HEARTBEAT_FILE.write_text(now)

    log("Heartbeat written")


def cmd_idle_push(args):
    """Auto-push when idle (called by cron). Only push if idle long enough."""
    idle_minutes = getattr(args, "idle_minutes", 10)
    threshold = time.time() - idle_minutes * 60

    # Check gateway activity (session files modified recently)
    recent_sessions = []
    if SESSIONS_DIR.exists():
        recent_sessions = [
            f for f in SESSIONS_DIR.glob("*.jsonl")
            if f.stat().st_mtime > threshold
        ]

    if recent_sessions:
        log(f"idle-push skipped: {len(recent_sessions)} active sessions")
        return

    # Check if we pushed recently (don't push more than once per idle_minutes)
    if HA_HEARTBEAT_FILE.exists():
        try:
            last_push = HA_HEARTBEAT_FILE.stat().st_mtime
            if last_push > threshold:
                log("idle-push skipped: pushed recently")
                return
        except OSError:
            pass

    if not pi_reachable():
        log("idle-push skipped: Pi unreachable")
        return

    log("idle-push: starting")
    cmd_push(args)


def cmd_sync_version(args):
    """Check Hermes version parity between nodes."""
    local = hermes_version()
    print(f"  WSL:  {local}")

    if not pi_reachable():
        print("  Pi:   UNREACHABLE")
        return

    remote = hermes_version_pi()
    print(f"  Pi:   {remote}")
    if local == remote:
        print("  Status: MATCH")
    else:
        print("  Status: MISMATCH")
        log(f"Version mismatch: WSL={local}, Pi={remote}", "WARN")


def cmd_events(args):
    """Show recent HA events."""
    n = getattr(args, "last", 20)
    if not HA_EVENTS_LOG.exists():
        print("No events recorded yet.")
        return

    try:
        with open(HA_EVENTS_LOG, "r") as f:
            lines = f.readlines()
    except OSError:
        return

    for line in lines[-n:]:
        print(line.rstrip())


# ── CLI ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Hermes Agent HA v3 — File-First Sync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # init-node
    p = sub.add_parser("init-node", help="Initialize node identity")
    p.add_argument("--role", choices=["primary", "standby"], default=None,
                   help="Node role (default: standby)")
    p.add_argument("--force", action="store_true",
                   help="Re-initialize without confirmation")

    # status
    sub.add_parser("status", help="Show HA status")

    # push
    p = sub.add_parser("push", help="Full sync to Pi")
    p.add_argument("--dry-run", action="store_true", help="Show what would be synced")

    # rebuild
    p = sub.add_parser("rebuild", help="Rebuild local state.db from sessions/")
    p.add_argument("--all", action="store_true", help="Rebuild everything")

    # takeover
    sub.add_parser("takeover", help="Pull from Pi, rebuild, become primary")

    # handoff
    sub.add_parser("handoff", help="Push to Pi, stop gateway, demote")

    # heartbeat
    sub.add_parser("heartbeat", help="Write heartbeat to Pi")

    # idle-push
    p = sub.add_parser("idle-push", help="Auto-push when idle")
    p.add_argument("--idle-minutes", type=int, default=10,
                   help="Minimum idle time before pushing (default: 10)")

    # sync-version
    sub.add_parser("sync-version", help="Check version parity")

    # events
    p = sub.add_parser("events", help="Show recent HA events")
    p.add_argument("--last", type=int, default=20, help="Number of events to show")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "init-node": cmd_init_node,
        "status": cmd_status,
        "push": cmd_push,
        "rebuild": cmd_rebuild,
        "takeover": cmd_takeover,
        "handoff": cmd_handoff,
        "heartbeat": cmd_heartbeat,
        "idle-push": cmd_idle_push,
        "sync-version": cmd_sync_version,
        "events": cmd_events,
    }

    try:
        commands[args.command](args)
    except Exception as e:
        log(f"{args.command} failed: {e}", "ERROR")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
