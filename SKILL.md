---
name: agent-ha
description: Hermes Agent hot-standby HA system. WSL as primary (when online), Pi as always-on backup. Automatic data sync, gateway failover, and brain-split recovery.
---

# Agent HA — Hot Standby System

## Architecture

```
  ┌──────────────────┐         ┌──────────────────┐
  │   WSL (Primary)  │ ──SSH── │   Pi (Standby)   │
  │   When Online    │  ────→  │   Always Online   │
  │   x86_64, fast   │  ←──── │   arm64, slower   │
  └──────────────────┘  can't  └──────────────────┘
         │              reach       │
         │              WSL         │
    ┌────┴────┐                   ┌─┴──┐
    │ Gateway │                   │ GW │  (only one active)
    │ ACTIVE  │                   │ OFF │
    └─────────┘                   └────┘
```

**Rules:**
1. WSL is primary when online (stronger performance)
2. Pi takes over when WSL goes offline (always-on)
3. Only one gateway active at a time (prevent channel conflicts)
4. All sync initiated by WSL → Pi (Pi cannot reach WSL behind NAT)
5. Data flows bidirectionally via merge, never overwrite-and-lose

## State Machine

```
                WSL boots up
                     │
                     ▼
              ┌──────────────┐
              │   takeover   │ ── pull Pi data, merge, become primary
              └──────┬───────┘
                     │
                     ▼
         ┌───────────────────────┐
         │  WSL=Primary running  │ ←── periodic push every 30min
         │  Pi=Standby (GW off)  │
         └───────────┬───────────┘
                     │ WSL goes offline
                     ▼
              ┌──────────────┐
              │   handoff    │ ── push data, stop local GW, start Pi GW
              └──────┬───────┘
                     │
                     ▼
         ┌───────────────────────┐
         │  Pi=Primary running   │
         │  WSL=Offline          │
         └───────────┴───────────┘
                     │ WSL comes back
                     └──── loop back to takeover
```

## All Scenarios Covered

| # | Scenario | What happens |
|---|----------|-------------|
| A | WSL online, Pi online | WSL=primary, Pi=standby. Cron pushes data every 30min, heartbeat every 2min |
| B | WSL online, Pi offline | WSL=primary standalone. Push/heartbeat fails silently, retry next cycle |
| C | WSL offline, Pi online | Pi detects heartbeat timeout (3min), auto-promotes to primary, starts gateway |
| D | WSL offline, Pi offline | No service. Data preserved locally |
| E | Both online, both have gateway | Pi watchdog detects WSL heartbeat → stops Pi GW (safety net) |
| F | WSL crashes (no handoff) | Pi detects heartbeat timeout → auto-promotes, starts GW (3min max delay) |
| G | Pi crashes while standby | WSL continues. Pi syncs when recovered |
| H | Network split (both think primary) | takeover merges both sides' data. Last-write-wins for text, row-merge for SQLite |

## Data Sync Strategy

### By data type

| Category | Data | Strategy | Why |
|----------|------|----------|-----|
| **SQLite** | state.db, memory_store.db | Row-level merge | Both sides may accumulate new data during split |
| **Text** | MEMORY.md, USER.md, SOUL.md | Latest mtime wins | Single-file, last-write semantics |
| **Dirs** | skills/, sessions/ | rsync bidirectional (add-only) | Append-only, no conflicts |
| **Channels** | weixin/, pairing/, channel_directory.json | Primary-only push | Only active GW writes these |
| **Shared Config** | config.shared.yaml | Primary push + config_merge | Keeps behavioral config identical on both sides |

### Config sync (shared vs environment-specific)

`config.shared.yaml` contains core behavioral sections that define "who the agent is":
- memory, plugins, skills, cron, agent, browser, model, tts, stt, voice, etc.
- Synced from WSL (primary) to Pi during push/takeover/handoff
- Applied via `config_merge.py` which overlays shared sections onto config.yaml

`config.yaml` retains environment-specific sections on each side:
- `command_allowlist` — security policy (may differ per environment)
- `security` — tirith_enabled etc. (environment security level)
- `mcp_servers` — may have different connect_timeout per environment
- `_config_version`, `dashboard`, `bedrock` — internal/version-specific

**Workflow for config changes:**
1. Edit config on WSL (primary)
2. If you changed a core section (memory, skills, etc.): regenerate shared → `python3 ~/.hermes/skills/devops/agent-ha/scripts/config_merge.py --check` → push
3. Cron push (every 30min) auto-syncs shared config to Pi

### SQLite merge logic

Used by ALL sync paths: `push` (cron), `takeover`, `handoff`. No exceptions.

```
Pi DB + Local DB → Merged DB
  - For each table, INSERT rows from Pi that don't exist in local (by PK)
  - PK collision → keep local row (INSERT OR IGNORE)
  - Push merged result to Pi FIRST, then replace local (atomic ordering)
  - No data loss: only ADD, never DELETE
```

**Why merge even during cron push:** Standby Pi may accumulate new data (e.g. Holographic memory on_pre_compress hooks). Direct scp overwrite would destroy those rows. Bidirectional merge preserves everything.

## Commands

### ha_sync.py v2 — Core sync script (Node-Aware)

Located at `~/.hermes/skills/devops/agent-ha/scripts/ha_sync.py`

**v2 changes (2026-04-18):**
- Auto node detection (WSL vs Pi) via `/proc/device-tree/model`, `/proc/version`, `uname -m`
- `~/.hermes/.ha_node` — node metadata (name, type, platform, arch, model, ips, hermes_version)
- `~/.hermes/.ha_peer` — cached peer info (discovered via SSH)
- `~/.hermes/.ha_events.jsonl` — event log (failover, takeover, handoff, etc.)
- Epoch counter in state (prevents split-brain)
- All output includes node identity labels (e.g., `[wsl|x86_64]`, `[pi|RPi 4 Model B Rev 1.4|aarch64]`)
- User notification on role changes (`hermes chat -q`)
- New commands: `init-node`, `events` (-n count, -t type filter)

Environment variables (optional, override defaults):
```bash
export HA_PI_HOST=PI_IP_PLACEHOLDER    # Pi IP
export HA_PI_USER=ha_user           # Pi username
export HA_HEARTBEAT_STALE=180      # Heartbeat stale threshold (seconds)
```

File locking: uses `~/.hermes/.ha_lock` (fcntl LOCK_EX|LOCK_NB). Concurrent runs are rejected.

```bash
# Initialize/update node identity (run once on each node)
python3 ha_sync.py init-node

# View current state (includes node identity, epoch, peer status)
python3 ha_sync.py status

# WSL comes online → become primary
python3 ha_sync.py takeover

# WSL going offline → hand off to Pi
python3 ha_sync.py handoff

# Periodic backup (cron, every 30min)
python3 ha_sync.py push

# Lightweight heartbeat (cron, every 2min) — signals local node is alive to peer
python3 ha_sync.py heartbeat

# Check and fix version mismatch between sides
python3 ha_sync.py sync-version

# Manual merge of specific DB
python3 ha_sync.py merge-db state.db

# Show recent HA events
python3 ha_sync.py events
python3 ha_sync.py events -n 50 -t failover
```

### SSH Key Auth (required)

ha_sync.py uses passwordless SSH. Setup once:
```bash
ssh-keygen -t ed25519 -C 'hermes-ha@wsl' -f ~/.ssh/id_ed25519 -N ''
ssh-copy-id ha_user@PI_IP_PLACEHOLDER
# Verify:
ssh -o BatchMode=yes ha_user@PI_IP_PLACEHOLDER 'echo OK'
```

## Setup

### 1. SSH Key Auth (do this first)

```bash
ssh-keygen -t ed25519 -C 'hermes-ha@wsl' -f ~/.ssh/id_ed25519 -N ''
ssh-copy-id ha_user@PI_IP_PLACEHOLDER
# Verify: ssh -o BatchMode=yes ha_user@PI_IP_PLACEHOLDER 'echo OK'
```

Also add to `~/.ssh/config`:
```
Host pi
    HostName PI_IP_PLACEHOLDER
    User ha_user
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30
    ConnectTimeout 5
```

### 2. Deploy sync script to WSL

```bash
# Script is at ~/.hermes/skills/devops/agent-ha/scripts/ha_sync.py
# Uses SSH key auth (no password stored)
chmod +x ~/.hermes/skills/devops/agent-ha/scripts/ha_sync.py
```

### 3. WSL cron — periodic push + heartbeat

```bash
mkdir -p ~/.hermes/logs
(crontab -l 2>/dev/null; echo "*/30 * * * * /usr/bin/python3 ~/.hermes/skills/devops/agent-ha/scripts/ha_sync.py push >> ~/.hermes/logs/ha_push.log 2>&1") | crontab -
# Heartbeat every 2 minutes — lightweight SSH, signals WSL is alive to Pi watchdog
(crontab -l 2>/dev/null; echo "*/2 * * * * /usr/bin/python3 ~/.hermes/skills/devops/agent-ha/scripts/ha_sync.py heartbeat >> ~/.hermes/logs/ha_heartbeat.log 2>&1") | crontab -
```

### 4. Pi gateway systemd service

Deploy from WSL:
```bash
# Create service file locally first
cat > /tmp/hermes-gateway.service << 'EOF'
[Unit]
Description=Hermes Agent Gateway
After=network-online.target

[Service]
Type=simple
Environment=PATH=~/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=~/
ExecStart=~/.local/bin/hermes gateway run --replace
Restart=on-failure
RestartSec=10
WorkingDirectory=~/

[Install]
WantedBy=default.target
EOF

# Deploy and activate
scp /tmp/hermes-gateway.service ha_user@PI_IP_PLACEHOLDER:~/.config/systemd/user/
ssh ha_user@PI_IP_PLACEHOLDER 'systemctl --user daemon-reload && systemctl --user enable hermes-gateway.service && loginctl enable-linger ha_user'
```
**IMPORTANT:** Keep Environment= PATH short! Long PATH with node/bin caused 216/GROUP error.
**IMPORTANT:** Must `systemctl --user enable` (not just daemon-reload). Without enable, Restart=on-failure won't work.

### 5. Pi watchdog + cron

Deploy watchdog from WSL (avoids heredoc quoting issues):
```bash
# Create watchdog locally, then scp to Pi
# Watchdog checks .ha_state every minute:
#   Pi is primary → start gateway via systemd
#   Pi is standby → stop gateway (safety net)
scp ~/.hermes/skills/devops/agent-ha/scripts/ha_watchdog.sh ha_user@PI_IP_PLACEHOLDER:~/ha_watchdog.sh
ssh ha_user@PI_IP_PLACEHOLDER 'chmod +x ~/ha_watchdog.sh && (echo "* * * * * ~/ha_watchdog.sh" | crontab -)'
```

### 6. WSL systemd auto-takeover

```ini
# ~/.config/systemd/user/hermes-ha-takeover.service
[Unit]
Description=Hermes HA Auto-Takeover
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 ~/.hermes/skills/devops/agent-ha/scripts/ha_sync.py takeover
RemainAfterExit=yes
TimeoutStartSec=120

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable hermes-ha-takeover.service
```

## Version Sync Strategy

Keep both sides on the same hermes version to avoid config.yaml structural drift.

### Checking versions

`status` command shows version comparison:
```
Version: WSL=0.10.0  Pi=0.10.0  [OK]
```

### Fixing version mismatch

```bash
python3 ha_sync.py sync-version
# Auto-detects which side is older, runs hermes update + config migrate on it
```

### After hermes update on WSL

1. Stop gateway first: `systemctl --user stop hermes-gateway.service`
2. Run `hermes update`
3. Handle stash conflicts if any: `cd ~/.hermes/hermes-agent && git stash pop` (may need `--reject` then manual fix)
4. Run `hermes config migrate`
5. Restart gateway: `systemctl --user start hermes-gateway.service`
6. Run `sync-version` to update Pi too
7. Run `push` to sync data

### Config.yaml sync policy

Uses `config.shared.yaml` + `config_merge.py` for automatic config sync:

- **Shared sections** (memory, plugins, skills, cron, model, tts, etc.) — defined in `config.shared.yaml`, auto-synced during push/takeover/handoff
- **Environment-specific sections** (command_allowlist, security, mcp_servers) — intentionally different per side, never overwritten
- **Internal sections** (_config_version, dashboard, bedrock) — managed by hermes itself

**To add a new shared section:** edit it in WSL's config.yaml, then regenerate shared:
```bash
python3 ~/.hermes/skills/devops/agent-ha/scripts/gen_shared.py  # regenerates config.shared.yaml
python3 ~/.hermes/skills/devops/agent-ha/scripts/config_merge.py --check  # verify
python3 ~/.hermes/skills/devops/agent-ha/scripts/ha_sync.py push  # sync to Pi
```

**To check config drift:**
```bash
python3 ~/.hermes/skills/devops/agent-ha/scripts/config_merge.py --check
```

### config_merge.py — Config overlay merge

Located at `~/.hermes/skills/devops/agent-ha/scripts/config_merge.py`

Overlays `config.shared.yaml` sections onto `config.yaml`, preserving environment-specific sections.

```bash
# Check what would change (dry)
python3 config_merge.py --check

# Apply merge
python3 config_merge.py

# Dry run (print changes without writing)
python3 config_merge.py --dry-run
```

Sections from shared: memory, plugins, skills, cron, agent, browser, model, tts, stt, voice, terminal, display, delegation, etc.
Sections preserved: command_allowlist, security, mcp_servers, _config_version, dashboard, bedrock.

## Brain-Split Recovery

If both sides ran independently and accumulated data:

1. WSL comes online, runs `takeover`
2. Script pulls Pi's databases, merges row-by-row
3. New sessions from Pi are added to WSL's state.db
4. New facts from Pi are added to WSL's memory_store.db
5. Text files: latest mtime wins
6. Skills/sessions: additive merge (never deletes)
7. Result is pushed back to Pi

**Data loss risk:** Minimal. Only scenario is if both sides independently modified the *same text file* (MEMORY.md) — last mtime wins. SQLite is fully merge-safe.

## Deployment Status

- [x] ha_sync.py written and deployed to WSL
- [x] `status` command tested — works
- [x] `push` command tested — works (syncs SQLite + channel state to Pi)
- [x] `heartbeat` command — lightweight heartbeat (WSL alive signal to Pi)
- [x] `takeover` command tested — works (merges Pi data, stops Pi GW, starts WSL GW)
- [x] `handoff` command tested — works (pushes data, stops WSL GW, starts Pi GW via systemd)
- [x] Pi watchdog (`ha_watchdog.sh`) — deployed to Pi, cron every minute, **heartbeat-based failover**
- [x] WSL cron (periodic push) — set up, every 30 minutes
- [x] WSL cron (heartbeat) — set up, every 2 minutes
- [x] WSL systemd auto-takeover service — enabled (hermes-ha-takeover.service)
- [x] SSH key auth — set up (ed25519, passwordless)
- [x] Pi gateway systemd service — deployed and **enabled** (hermes-gateway.service)
- [x] Pi loginctl enable-linger — enabled (user services survive logout/reboot)
- [x] End-to-end test: handoff → verify Pi primary → takeover → verify WSL primary
- [x] End-to-end test: heartbeat failover → verify Pi auto-promotes on WSL offline

## Pitfalls & Lessons

1. **Pi cannot reach WSL** — WSL is behind NAT (172.21.x.x). All sync must be WSL-initiated. Pi's `ha_watchdog.sh` can only check local state, cannot pull data from WSL.
2. **Pi gateway systemd user service** — needs `loginctl enable-linger` for user services to survive logout/reboot. Error code 216/GROUP means bad Environment= line in service file.
3. **Pi venv has no pip** — installed via `uv`, not standard pip. Don't try `pip install -e .` on Pi. The hermes binary works fine as-is.
4. **state.db schema v6** is compatible between v0.9.0 (WSL) and v0.10.0 (Pi) — confirmed.
5. **`hermes gateway run --replace`** is the correct command to start gateway on both sides. `hermes gateway start` only works with systemd service.
6. **WSL systemd service for gateway** already exists (`hermes-gateway.service`), auto-starts on login. HA takeover should restart it, not create a new one.
7. **SCP mtime issue** — scp overwrites file timestamps, making the destination appear "newer" in status check. Use `rsync -a` (preserves timestamps) for the merged DB push-back to avoid this.
8. **systemctl vs nohup** — systemd user service needs simple PATH. Long PATH with node/bin etc. caused 216/GROUP error. Keep Environment= lines short.
9. **hermes update with local changes — full recovery procedure:**
   ```bash
   systemctl --user stop hermes-gateway.service          # 1. Stop gateway first
   hermes update                                         # 2. Update (auto-stashes local changes)
   cd ~/.hermes/hermes-agent
   git stash show                                        # 3. Check what was stashed
   git stash pop                                         # 4. Pop stash (may conflict)
   # If conflicts: use --reject fallback
   git stash show -p | git apply --reject --whitespace=nowarn
   # Fix .rej files manually (grep for context in new version, re-patch)
   rm -f **/*.rej
   hermes config migrate                                 # 5. Migrate config to new version
   systemctl --user start hermes-gateway.service         # 6. Restart gateway
   # 7. Sync to Pi
   python3 ~/.hermes/skills/devops/agent-ha/scripts/ha_sync.py push
   python3 ~/.hermes/skills/devops/agent-ha/scripts/ha_sync.py sync-version
   ```
   Key files that get local patches: `error_classifier.py` (Chinese rate-limit patterns), `run_agent.py` (holographic on_pre_compress), `holographic/__init__.py` (auto_extract), `holographic/plugin.yaml` (on_pre_compress hook).
10. **Deploy scripts via scp, not SSH heredoc** — nested quoting in `ssh 'cat > file << EOF'` is fragile. Create files locally, then `scp` to Pi.
11. **hermes update may change file offsets** — local patches (e.g. error_classifier.py Chinese rate-limit patterns) may fail if upstream moved code. Check `.rej` files, find correct line by `grep -n` for context, re-apply patch manually.
12. **Pi systemd HEREDOC variable expansion** — `<< SERVICE_EOF` (unquoted) expands $HOME at deploy time. Use this for parameterization. `<< 'SERVICE_EOF'` (quoted) preserves $HOME literally — wrong for systemd templates.
13. **rsync --update for bidirectional sync** — plain `rsync -a` overwrites newer files on the receiving side. Adding `--update` (`rsync -a --update`) skips files that are newer on receiver, preventing accidental data overwrite during split-brain recovery.
14. **config change workflow** — after editing config on WSL, if core sections changed (memory/skills/approvals etc.), must regenerate shared + push to Pi, otherwise Pi keeps old values. Run `gen_shared.py` → `push`.
15. **CRITICAL: Pi gateway service must be `enabled`** — `systemctl --user enable hermes-gateway.service` is required for `Restart=on-failure` to work. Initial deployment only did `daemon-reload` but forgot `enable`. Without enable, the service is "disabled" — first failure is permanent, watchdog's `systemctl start` runs but the service won't auto-restart after crashes. Fix: `ssh pi 'systemctl --user enable hermes-gateway.service'`.
16. **WSL shutdown → Pi auto-failover via heartbeat** — WSL writes heartbeat timestamp to Pi every 2min. Pi watchdog (every 1min) checks heartbeat age. If stale > 180s, Pi auto-promotes to primary and starts gateway. Max failover delay: ~3 minutes. No need for explicit handoff before WSL shutdown. Takeover clears stale heartbeat to prevent false failover.
17. **Pi gateway first-start may fail with exit 1** — Observed at 09:12 CST 2026-04-18: Pi gateway started by watchdog but exited after 1s with code=exited, status=1. No clear error in journal (just "Hermes Gateway Starting..." then immediate exit). Possible causes: race condition with WSL takeover (WSL may have been shutting down Pi gateway simultaneously), or platform connection failure. Subsequent manual start worked fine. The `Restart=on-failure` + `enable` fix (pitfall #15) should handle this going forward.
18. **Config values in shared sections must be edited in config.shared.yaml too** — If you change a shared-section value (e.g. `persistent_retry_interval`) directly in `config.yaml`, the next `push` will overwrite it back from `config.shared.yaml`. Always edit both `config.yaml` AND `config.shared.yaml`, then push. Or edit only `config.shared.yaml`, then run `config_merge.py` to apply locally, then push.
19. **Epoch split-brain prevention (RESOLVED in v2)** — `resolve_epoch(peer_epoch)` calculates max(local, peer)+1. `check_split_brain()` detects dual-primary conflicts via SSH. `cmd_takeover()` reads peer epoch before claiming primary. `set_role()` accepts explicit `epoch=` parameter. Watchdog v2 writes epoch on promote. See code: `read_peer_epoch()`, `resolve_epoch()`, `check_split_brain()`.
20. **Python 3.13 .pyc recovery when source is overwritten** — If Pi writes v2 code but WSL push overwrites it with v1, `__pycache__/*.cpython-313.pyc` may still contain v2. Recovery: `import importlib.util; spec = importlib.util.spec_from_file_location("mod", "path/to.pyc"); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)`. Then extract via introspection: `[f for f in dir(mod) if callable(getattr(mod, f)) and hasattr(getattr(mod, f), "__code__")]` for functions, `fn.__code__.co_consts` for strings, `fn.__doc__` for docstrings. **CANNOT decompile** Python 3.13 bytecode — uncompyle6, decompyle3, pycdc all fail. Reconstruct from v1 source + extracted v2 metadata.
21. **CRITICAL: rsync -az overwrites newer files, destroying code updates on peer** — This was the ROOT CAUSE of Pi's v2 being silently replaced by WSL's v1 during takeover/push. `rsync -a` does NOT protect newer destination files. Fix (two layers): (a) `SYNC_EXCLUDE` list in ha_sync.py excludes 5 HA scripts from all rsync — code changes must be deployed explicitly via `scp`, never auto-synced. (b) `rsync -az --update` as safety net to skip files newer on receiver. `cmd_push()` now calls `sync_rsync_mirror()` instead of inline rsync, ensuring excludes apply everywhere.
22. **Watchdog demote gate: must check role=primary, not just gateway running** — Original watchdog v2 only entered the demote branch when `GW_RUNNING=YES`. If Pi was primary but gateway was already stopped (e.g. WSL takeover stopped it), watchdog would skip demote entirely — Pi stays "primary" in state even though WSL is online. Fix: separate the role demote check (`if [ "$ROLE" = "primary" ]`) from the gateway stop check (`if [ "$GW_RUNNING" = "YES" ]`). Always write standby state when WSL heartbeat is fresh, regardless of gateway status.
23. **CRITICAL: Cron DBUS_SESSION_BUS_ADDRESS missing — systemctl --user silently fails** — Cron jobs don't inherit `DBUS_SESSION_BUS_ADDRESS` or `XDG_RUNTIME_DIR` from the user session. Without these, `systemctl --user start/stop/is-active` all fail with "Failed to connect to user scope bus" but the script continues (pipes to /dev/null). The watchdog thinks gateway started when it actually didn't. Fix: auto-detect in ha_watchdog.sh header: `export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"`. This was the root cause of Pi failover failing silently for 10+ minutes — gateway promoted to PRIMARY but never actually started.
24. **Cron push SQLite must use merge, not scp overwrite** — Original `cmd_push()` used plain `scp` to copy SQLite DBs to Pi. If Pi standby accumulated new rows (e.g. Holographic memory hooks), scp overwrite would destroy them silently. Fix: `cmd_push()` now calls `merge_sqlite()` (same as takeover) — pulls Pi DB, merges rows via INSERT OR IGNORE, pushes merged result back. Trade-off: ~5s slower per push cycle due to merge processing, but eliminates silent data loss.
25. **iLink sendmessage API returns HTTP 200 `{}` but does NOT actually deliver without active WebSocket** — Calling `ilink/bot/sendmessage` directly via HTTP returns 200 with empty JSON `{}`, which looks like success (no `ret`/`errcode` keys), but the message is never delivered to the user. Messages only go through when gateway's WebSocket long-poll connection is active. **Do NOT use ha_send_weixin.py for notifications — it doesn't work.** `hermes chat -q` is the only way to send through the gateway's active WebSocket, but the agent processes the message (adds fluff). No reliable direct-push method exists without gateway running.
26. **Cron push SQLite must use merge, not scp overwrite**
27. **FTS5 index corruption in state.db — repair procedure** — `messages_fts` (FTS5 virtual table, `content=messages`) can corrupt due to interrupted writes (HA merge during failover, WSL sudden shutdown). Symptom: `session_search` returns "database disk image is malformed". Diagnosis: `PRAGMA integrity_check` shows ~100 errors on Tree 12 (messages_fts_data). **Original data is safe** — `sessions` and `messages` tables are unaffected; only the search index is broken. FTS tables are NOT in `MERGE_TABLES`, so HA sync (push/takeover/handoff) never touches them — each side must rebuild independently. Repair SQL (run on each node separately): `DELETE FROM messages_fts; INSERT INTO messages_fts(rowid, content) SELECT id, content FROM messages; REINDEX messages_fts;`. Verify with `SELECT count(*) FROM messages_fts`. Alternatively, drop and recreate: `DROP TABLE messages_fts; CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=id); INSERT INTO messages_fts(rowid, content) SELECT id, content FROM messages;`.
28. **Config YAML supports ${VAR} env interpolation** — Hermes config.yaml natively supports `${VAR}` references via `_expand_env_vars()` in `hermes_cli/config.py`. Values are resolved from `os.environ` at load time. `.env` file variables are loaded into environment before config parse. Use this for API keys: store key in `.env`, reference in config.yaml as `${GLM_API_KEY}`. Note: `mcp_servers` is an environment-specific section — not synced via `config.shared.yaml`. Each node must have its own `.env` with the key.
29. **CRITICAL: SCP state.db while gateway running causes corruption** — Never scp a rebuilt state.db to a node while its gateway is running. Pi's Holographic `on_pre_compress` hook writes to state.db even in standby mode. If scp overwrites the DB mid-write, the result is a corrupted file (including base tables, not just FTS). **Correct procedure to push a rebuilt DB:** (1) Stop gateway on target, (2) scp the rebuilt state.db, (3) Delete WAL/SHM files on target (stale WAL from pre-scp writes will corrupt even a valid replacement), (4) Restart gateway. Discovered 2026-04-18 when WSL's scp of rebuilt state.db to Pi produced corruption despite the source being integrity-ok.
30. **WAL/SHM files silently corrupt valid DB replacements** — SQLite WAL and SHM files persist alongside the main `.db` file. Replacing the `.db` via scp/cp leaves old `-wal`/`-shm` files. SQLite opens the new DB, finds old WAL, tries to replay it — corruption. Symptom: `PRAGMA integrity_check` returns "malformed" even though replacement passed check in isolation (open with `file:path?mode=ro` to verify). **Always delete after replacing any SQLite file:** `rm -f state.db-wal state.db-shm`.

## HA Testing Procedure

Systematic 6-step validation after any significant change to ha_sync.py, ha_watchdog.sh, or the HA architecture:

```
Step 1 — 基础检查: init-node + status + events (both sides)
Step 2 — 数据同步: push (验证 SYNC_EXCLUDE 保护)
         方法: 在 Pi 上给 ha_sync.py 打 marker → push → 检查 marker 保留
Step 3 — 心跳感知: heartbeat 写入 → Pi watchdog 手动运行 → 验证 WSL online 识别
Step 4 — Handoff: WSL→Pi 切换 → 验证 Pi primary + gateway active + epoch 递增
Step 5 — Takeover: WSL 夺回 → 验证 epoch 竞争(max+1) + DB 合并 + Pi gateway stopped
Step 6 — 状态一致性: 两端 SQLite MD5 + 文本文件 MD5 + HA scripts MD5
```

**验证命令（一键全量检查）：**
```bash
# 最终状态一致性
for db in memory_store.db state.db; do
    W=$(md5sum ~/.hermes/$db | awk '{print $1}')
    P=$(ssh pi "md5sum ~/.hermes/$db" | awk '{print $1}')
    echo "$db: $([ "$W" = "$P" ] && echo MATCH || echo DIFFER)"
done
for f in memories/MEMORY.md memories/USER.md SOUL.md; do
    W=$(md5sum ~/.hermes/$f | awk '{print $1}')
    P=$(ssh pi "md5sum ~/.hermes/$f" | awk '{print $1}')
    echo "$f: $([ "$W" = "$P" ] && echo MATCH || echo DIFFER)"
done
```

**常见时间差问题：** WSL heartbeat 写入和 Pi watchdog cron 之间存在 ~60s 窗口。测试时先写 heartbeat，再等 1 分钟让 cron 自然触发，或手动运行 watchdog 验证。

## Security Notes

- SSH key auth (ed25519) — no passwords stored anywhere
- Key: `~/.ssh/id_ed25519`, deployed to Pi's `authorized_keys`
- Channel credentials are synced between sides — same bot token, same auth
- PI_HOST/PI_USER read from env vars `HA_PI_HOST`/`HA_PI_USER` (defaults in code for convenience)
- File locking via fcntl prevents concurrent ha_sync instances from corrupting data
- All shell commands use shlex.quote() to prevent injection
- SQLite merge pushes to Pi FIRST, then replaces local (atomic ordering)
- Published to GitHub: https://github.com/gegewu81/hermes-ha
