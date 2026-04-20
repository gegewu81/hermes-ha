# Hermes HA — Hot-Standby High Availability System

[English](#english) | [中文](#中文)

---

## 中文

Hermes Agent 热备高可用系统 v3 — 文件优先同步架构。在两个节点之间实现自动故障转移和数据同步。

### 架构

```
  ┌──────────────────┐         ┌──────────────────┐
  │   主节点 (WSL)    │ ──SSH── │  备节点 (Raspberry Pi) │
  │   x86_64, 高性能  │  ────→  │  arm64, 常在线      │
  └──────────────────┘  单向    └──────────────────┘
         │                            │
    ┌────┴────┐                  ┌────┴────┐
    │ Gateway │                  │ Gateway │  (仅一个活跃)
    │ ACTIVE  │                  │ STANDBY │
    └─────────┘                  └─────────┘
```

- **主节点**: 在线时自动接管，性能更强的 x86 设备（如 WSL）
- **备节点**: 始终在线的低功耗设备（如 Raspberry Pi），主节点离线时自动接管
- **单向同步**: 所有数据同步由主节点发起（备节点通常位于 NAT 后面）
- **文件优先**: 不同步数据库，只同步源文件，本地重建 DB。DB 是可丢弃的，文件才是真相

### 核心功能

| 功能 | 说明 |
|------|------|
| 自动故障转移 | 心跳超时（默认 180s）后备节点自动提升为主节点 |
| 文件优先同步 | 同步 JSONL/Memory JSON/Config YAML，本地重建 state.db |
| 脑裂防护 | Epoch 计数器 + 角色竞争机制防止双主 |
| 事件日志 | 日志格式记录所有故障转移、接管、交接事件 |
| 节点感知 | 自动检测节点类型（WSL/Pi），日志和状态包含节点标识 |

### 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/gegewu81/hermes-ha.git
cd hermes-ha
git checkout v3

# 2. 设置环境变量
export HA_PI_HOST=<你的Pi的SSH别名或IP>
export HA_PI_USER=<Pi用户名>

# 3. 配置 SSH 免密登录
ssh-copy-id <HA_PI_USER>@<HA_PI_HOST>

# 4. 初始化节点
python3 scripts/ha_sync.py init-node --role primary   # WSL
python3 scripts/ha_sync.py init-node --role standby   # Pi

# 5. 初始同步
python3 scripts/ha_sync.py push

# 6. 查看状态
python3 scripts/ha_sync.py status
```

### 文件结构

```
hermes-ha/
├── scripts/
│   ├── ha_sync.py            # 核心同步脚本（10个子命令）
│   ├── ha_rebuild_remote.py  # 远程重建 helper
│   ├── ha_watchdog.sh        # Pi 端看门狗（cron 每分钟）
│   └── ha_notify.sh          # 通知脚本
├── SKILL.md                  # 完整技术文档（含所有 pitfalls）
└── README.md                 # 本文件
```

### 环境要求

- Python 3.10+（主节点）
- Bash 4+（备节点）
- SSH 免密登录（ed25519）
- rsync（数据传输）

### 数据同步策略

| 数据类型 | 策略 |
|----------|------|
| Sessions | rsync `--update`（最新胜出） |
| Memory DB | 导出为 JSON → SCP → 导入（不直接同步 .db） |
| State DB | 不同步，各节点从 JSONL 本地重建 |
| 文本文件 | SCP 最新 mtime 胜出 |
| Skill 文件 | rsync mirror（主→备） |

### 所有覆盖场景

| 场景 | 行为 |
|------|------|
| 主节点在线，备节点在线 | 主节点运行，定时推送 + 心跳 |
| 主节点离线，备节点在线 | 心跳超时后备节点自动提升，启动网关 |
| 两个节点同时在线但都有网关 | 备节点看门狗检测到心跳 → 停止备网关 |
| 网络分区（脑裂） | Epoch 竞争防止双主 |

### 详细文档

完整技术文档（部署步骤、所有 pitfalls、迁移指南）见 [SKILL.md](SKILL.md)。

### License

MIT

---

## English

Hot-standby high availability system v3 for Hermes Agent — file-first sync architecture.

### Architecture

```
  ┌──────────────────┐         ┌──────────────────┐
  │   Primary (WSL)   │ ──SSH── │  Standby (Raspberry Pi) │
  │   x86_64, fast    │  ────→  │  arm64, always-on       │
  └──────────────────┘  one-way └──────────────────┘
         │                            │
    ┌────┴────┐                  ┌────┴────┐
    │ Gateway │                  │ Gateway │  (only one active)
    │ ACTIVE  │                  │ STANDBY │
    └─────────┘                  └─────────┘
```

- **Primary**: Auto-takes over when online
- **Standby**: Always-on low-power device, auto-promotes on primary failure
- **One-way sync**: All data sync initiated by primary (standby usually behind NAT)
- **File-first**: Sync source files, rebuild DBs locally. DBs are disposable, files are truth

### Core Features

| Feature | Description |
|---------|-------------|
| Auto Failover | Standby auto-promotes on heartbeat timeout (default 180s) |
| File-First Sync | Sync JSONL/Memory JSON/Config YAML, rebuild state.db locally |
| Split-Brain Prevention | Epoch counter prevents dual-primary |
| Event Logging | Log-formatted event history for all failover/handoff events |
| Node Awareness | Auto-detect node type, logs include node identity |

### Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/gegewu81/hermes-ha.git
cd hermes-ha
git checkout v3

# 2. Set environment variables
export HA_PI_HOST=<your-pi-ssh-alias-or-ip>
export HA_PI_USER=<pi-username>

# 3. Set up SSH key auth
ssh-copy-id <HA_PI_USER>@<HA_PI_HOST>

# 4. Initialize nodes
python3 scripts/ha_sync.py init-node --role primary   # WSL
python3 scripts/ha_sync.py init-node --role standby   # Pi

# 5. Initial sync
python3 scripts/ha_sync.py push

# 6. Check status
python3 scripts/ha_sync.py status
```

### File Structure

```
hermes-ha/
├── scripts/
│   ├── ha_sync.py            # Core sync script (10 subcommands)
│   ├── ha_rebuild_remote.py  # Remote rebuild helper
│   ├── ha_watchdog.sh        # Pi watchdog (cron every minute)
│   └── ha_notify.sh          # Notification script
├── SKILL.md                  # Full technical docs (all pitfalls)
└── README.md                 # This file
```

### Requirements

- Python 3.10+ (primary node)
- Bash 4+ (standby node)
- SSH passwordless login (ed25519)
- rsync (data transfer)

### Data Sync Strategy

| Data Type | Strategy |
|-----------|----------|
| Sessions | rsync `--update` (newest wins) |
| Memory DB | Export to JSON → SCP → Import (never sync .db directly) |
| State DB | Never synced. Rebuilt from JSONL on each node |
| Text files | SCP latest-mtime-wins |
| Skill files | rsync mirror (primary → standby) |

### License

MIT
