---
name: agent-ha-file
description: Hermes Agent HA v3 — file-first sync. No DB merge, no schema dependency. Sync source files (sessions JSONL, memory JSON, config YAML), rebuild DBs locally after sync.
version: 3.0
---

# Agent HA v3 — File-First Sync

## Philosophy

v1/v2 tried to merge SQLite row-by-row across nodes. This is fragile:
- Schema differences between Hermes versions
- BLOB serialization bugs
- Watermark state drift
- ~700 lines of merge code for 2 DBs

v3 radical simplification: **sync source files, rebuild DBs locally.**

```
  Source of Truth           Sync (rsync/scp)         Derived (rebuild)
  ─────────────────         ──────────────           ─────────────────
  sessions/*.jsonl   ──→    both nodes have    ──→   state.db (local rebuild)
  memory_store.db    ──→    all source files   ──→   memory_store.db (export→import)
  config.yaml        ─→     latest wins        ─→    (direct use)
  SOUL.md            ─→     latest wins        ─→    (direct use)
  memory.json        ─→     latest wins        ─→    (direct use)
```

**Rule: never merge DBs. DBs are disposable, files are truth.**

## Architecture

```
  ┌──────────────────┐         ┌──────────────────┐
  │   WSL (Primary)  │ ──SSH── │   Pi (Standby)   │
  │   When Online    │  ────→  │   Always Online   │
  └──────────────────┘         └──────────────────┘
         │                            │
    ┌────┴────┐                  ┌────┴────┐
    │ Gateway │                  │ Gateway │  (only one active)
    │ ACTIVE  │                  │ STANDBY │
    └─────────┘                  └─────────┘

  Sync flow (always WSL → Pi, Pi can't reach WSL):
    1. rsync sessions/ → Pi (bidirectional, newest wins)
    2. Export memory → JSON, scp to Pi
    3. SCP text files (config, SOUL, memory) — latest mtime wins
    4. SSH: Pi rebuilds state.db from sessions/
    5. SSH: Pi imports memory_store.json → memory_store.db
```

## Commands

| Command | Description |
|---------|-------------|
| `ha_sync.py init-node [--role primary\|standby]` | Initialize node identity |
| `ha_sync.py status` | Show HA state, sync health, file counts |
| `ha_sync.py push` | Full sync: files → Pi, trigger remote rebuild |
| `ha_sync.py takeover` | Pull Pi data, rebuild local, become primary |
| `ha_sync.py handoff` | Push data to Pi, stop local GW, demote |
| `ha_sync.py heartbeat` | Write heartbeat timestamp to Pi |
| `ha_sync.py idle-push` | Auto-push when idle (called by cron) |
| `ha_sync.py sync-version` | Check Hermes version parity |
| `ha_sync.py events` | Show recent HA events |
| `ha_sync.py rebuild` | Rebuild local state.db from sessions/ |

## Sync Strategy by File Type

| Type | Files | Strategy |
|------|-------|----------|
| Sessions | `sessions/*.jsonl` | rsync --update (both directions, newest wins) |
| Memory DB | `memory_store.db` → `memory_export.json` | Export→SCP→Import (never sync .db directly) |
| State DB | `state.db` | Never synced. Rebuilt from JSONL on each node (**lossy** — see pitfalls) |
| Text files | `config.yaml`, `SOUL.md`, `memory.json` | SCP latest-mtime-wins |
| Skill files | `skills/**` | rsync mirror (primary → standby) |

## Files

```
scripts/
  ha_sync.py            — Main sync script (10 subcommands)
  ha_rebuild_remote.py  — Remote rebuild helper (scp'd to Pi on push)
  ha_watchdog.sh        — Pi standby watchdog (cron, auto-promote if primary dead)
  ha_notify.sh          — Notification helper (bell + notify-send + log)
```

## Cron Setup

**WSL (Primary):**
```
* * * * * ha_sync.py heartbeat >> logs/ha_heartbeat.log 2>&1
* * * * * ha_sync.py idle-push >> logs/ha_push.log 2>&1
```

**Pi (Standby):**
```
* * * * * ha_watchdog.sh >> logs/ha_watchdog.log 2>&1
```

## Migration from v2

1. Stop all cron jobs on both nodes
2. Install new scripts (ha_sync.py, ha_watchdog.sh)
3. Run `ha_sync.py init-node --role primary` on WSL
4. Run `ha_sync.py init-node --role standby` on Pi (via SSH)
5. Run `ha_sync.py push` for initial full sync
6. Re-enable cron jobs with new paths
7. Old ha_sync.py (v2) can be kept as .bak — no shared state

## Pitfalls

1. **Pi can't reach WSL** — all sync must originate from WSL
2. **DB is disposable** — if in doubt, `ha_sync.py rebuild` from JSONL
3. **Memory JSON must be exported first** — don't rsync memory_store.db directly
4. **rsync --update** — never --delete for sessions, we want newest from both sides
5. **Epoch counter** — prevents split-brain during failover, same as v2
6. **⚠️ Rebuild is LOSSY** — Hermes writes most sessions directly to state.db, NOT to JSONL files. Only ~8 JSONL files existed vs 288 sessions in DB. Rebuild only captures JSONL-available history. Use rebuild only for disaster recovery when state.db is corrupted/missing.
7. **Pi has no sqlite3 CLI** — remote rebuild uses Python sqlite3 module (via `ha_rebuild_remote.py`). Don't try `ssh pi 'sqlite3 ...'`.
8. **Sessions are JSONL format** — one JSON object per line, NOT pretty-printed JSON. Each line is `{role, content, ...}`.
9. **FTS5 must be in schema** — both local `ha_sync.py` and remote `ha_rebuild_remote.py` need FTS5 CREATE VIRTUAL TABLE in their schema SQL. Missing FTS5 = `session_search` tool breaks.
10. **Remote rebuild: scp script, not inline Python** — embedding Python via SSH with f-string interpolation causes escaping nightmares. Solution: `scp ha_rebuild_remote.py` → `ssh pi 'python3 /tmp/ha_rebuild_remote.py'`.
11. **push triggers full remote rebuild** — the `push` command deletes Pi's state.db and rebuilds from scratch. If Pi's gateway is running, this WILL cause errors. Stop Pi gateway before push, or accept brief disruption.
12. **sync-version hangs when Pi unreachable** — `cmd_sync_version()` calls `hermes_version_pi()` without first checking `pi_reachable()`. SSH timeout (10s) blocks the entire command. Fix: add `pi_reachable()` guard at the top, print "Pi: UNREACHABLE" if down.
13. **init-node crashes in non-interactive mode** — `cmd_init_node()` uses `input()` for re-init confirmation. Called from cron/pipe, this raises `EOFError`. Fix: detect `sys.stdin.isatty()` or add `--force` flag.
14. **v2 state files vs v3 state files** — v2 used `~/.hermes/.ha_state`, `~/.hermes/.ha_node`, `~/.hermes/.ha_events.jsonl`. v3 uses `~/.hermes/.ha/node.json`, `~/.hermes/.ha/epoch`, `~/.hermes/.ha/events.log`. Both sets may coexist after migration. v3 is self-contained under `.ha/` directory.
15. **v2 cron must be fully removed before adding v3 cron** — old cron pointed to `devops/agent-ha/scripts/ha_sync.py` (v2, broken syntax at line 1046). Must `crontab -r` and recreate with v3 path `devops/agent-ha-file/scripts/ha_sync.py`.
