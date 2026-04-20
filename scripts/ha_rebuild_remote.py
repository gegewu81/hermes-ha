#!/usr/bin/env python3
"""
Remote rebuild script for Hermes HA v3.
Run on Pi after sync: rebuilds state.db from sessions JSONL,
imports memory_export.json into memory_store.db.

Usage:
    python3 ha_rebuild_remote.py [--hermes-dir ~/.hermes]
"""

import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

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

MEMORY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL UNIQUE,
    category TEXT DEFAULT 'general',
    tags TEXT DEFAULT '',
    trust_score REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector BLOB
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE TABLE IF NOT EXISTS memory_banks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_facts_trust ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    content, tags,
    content=facts,
    content_rowid=fact_id
);
"""


def rebuild_state_db(sessions_dir: Path, state_db: Path):
    """Rebuild state.db from all JSONL session files."""
    if not sessions_dir.exists():
        print("No sessions directory found")
        return

    jsonl_files = sorted(sessions_dir.glob("*.jsonl"))
    if not jsonl_files:
        print("No JSONL session files found")
        return

    # Backup existing DB
    if state_db.exists():
        backup = state_db.with_suffix(".db.bak." + str(int(time.time())))
        shutil.copy2(state_db, backup)
        print(f"Backed up to {backup.name}")

    # Remove old DB
    state_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        p = state_db.parent / (state_db.name + suffix)
        p.unlink(missing_ok=True)

    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)

    msg_id = 0
    session_count = 0
    msg_count = 0

    for jf in jsonl_files:
        sid = jf.stem  # e.g. 20260412_124237_ae123112

        # Parse started_at from filename
        try:
            parts = sid.split("_")
            dt_str = parts[0] + "_" + parts[1]
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

                tool_calls = obj.get("tool_calls")
                tool_calls_str = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
                tool_name = obj.get("tool_name")

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

    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()

    print(f"Rebuilt state.db: {session_count} sessions, {msg_count} messages")
    print(f"Total: {total_sessions} sessions, {total_messages} messages")


def import_memory_json(mem_json: Path, memory_db: Path):
    """Import memory_export.json into memory_store.db."""
    if not mem_json.exists():
        print("No memory_export.json found")
        return

    with open(mem_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    facts = data.get("facts", [])
    entities = data.get("entities", [])

    if not facts and not entities:
        print("No facts or entities in export")
        mem_json.unlink(missing_ok=True)
        return

    # Backup existing
    if memory_db.exists():
        backup = memory_db.with_suffix(".db.bak." + str(int(time.time())))
        shutil.copy2(memory_db, backup)
        print(f"Backed up memory_store.db to {backup.name}")

    memory_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        p = memory_db.parent / (memory_db.name + suffix)
        p.unlink(missing_ok=True)

    conn = sqlite3.connect(str(memory_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(MEMORY_SCHEMA_SQL)

    imported_facts = 0
    for fact in facts:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO facts "
                "(content, category, tags, trust_score, retrieval_count, helpful_count, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    fact.get("content"),
                    fact.get("category", "general"),
                    fact.get("tags", ""),
                    fact.get("trust_score", 0.5),
                    fact.get("retrieval_count", 0),
                    fact.get("helpful_count", 0),
                    fact.get("created_at"),
                    fact.get("updated_at"),
                ),
            )
            imported_facts += 1
        except Exception as e:
            print(f"  Skip fact: {e}")

    imported_entities = 0
    for ent in entities:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO entities (name, entity_type, aliases, created_at) "
                "VALUES (?,?,?,?)",
                (
                    ent.get("name"),
                    ent.get("entity_type", "unknown"),
                    ent.get("aliases", ""),
                    ent.get("created_at"),
                ),
            )
            imported_entities += 1
        except Exception as e:
            print(f"  Skip entity: {e}")

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    print(f"Imported memory: {imported_facts}/{len(facts)} facts, "
          f"{imported_entities}/{len(entities)} entities")

    # Clean up export file
    mem_json.unlink(missing_ok=True)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Remote rebuild for Hermes HA v3")
    parser.add_argument("--hermes-dir", default=None, help="Hermes home directory")
    args = parser.parse_args()

    hermes_dir = Path(args.hermes_dir) if args.hermes_dir else Path.home() / ".hermes"
    sessions_dir = hermes_dir / "sessions"
    state_db = hermes_dir / "state.db"
    memory_db = hermes_dir / "memory_store.db"
    mem_json = hermes_dir / "memory_export.json"

    print(f"Hermes dir: {hermes_dir}")
    print()

    print("[1/2] Rebuilding state.db...")
    rebuild_state_db(sessions_dir, state_db)
    print()

    print("[2/2] Importing memory...")
    import_memory_json(mem_json, memory_db)
    print()

    print("Remote rebuild complete.")


if __name__ == "__main__":
    main()
