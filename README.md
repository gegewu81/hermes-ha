# Hermes HA — Hot-Standby High Availability System

[English](#english) | [中文](#中文)

---

## 中文

Hermes Agent 热备高可用系统，用于在两个节点之间实现自动故障转移和数据同步。

### 架构

```
  ┌──────────────────┐         ┌──────────────────┐
  │   主节点 (WSL)    │ ──SSH── │  备节点 (Raspberry Pi) │
  │   x86_64, 高性能  │  ────→  │  arm64, 常在线      │
  └──────────────────┘  单向    └──────────────────┘
```

- **主节点**: 在线时自动接管，性能更强的 x86 设备（如 WSL）
- **备节点**: 始终在线的低功耗设备（如 Raspberry Pi），主节点离线时自动接管
- **单向同步**: 所有数据同步由主节点发起（备节点通常位于 NAT 后面）
- **数据安全**: 双向合并，永不覆盖丢失

### 核心功能

| 功能 | 说明 |
|------|------|
| 自动故障转移 | 心跳超时（默认 180s）后备节点自动提升为主节点 |
| 无损数据同步 | SQLite 行级合并，文本文件最新 mtime 胜出，目录增量 rsync |
| 配置同步 | 通过 `config.shared.yaml` 自动同步核心配置，保留环境特定设置 |
| 脑裂防护 | Epoch 计数器 + 角色竞争机制防止双主 |
| 事件日志 | JSONL 格式记录所有故障转移、接管、交接事件 |
| 节点感知 | 自动检测节点类型（WSL/Pi），日志和状态包含节点标识 |

### 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/gegewu81/hermes-ha.git

# 2. 设置环境变量
export HA_PI_HOST=<你的Pi的IP>
export HA_PI_USER=<你的Pi用户名>

# 3. 配置 SSH 免密登录
ssh-keygen -t ed25519 -C 'hermes-ha@wsl' -f ~/.ssh/id_ed25519 -N ''
ssh-copy-id <YOUR_PI_USER>@<YOUR_PI_IP>

# 4. 初始化节点
python3 scripts/ha_sync.py init-node

# 5. 查看状态
python3 scripts/ha_sync.py status
```

### 文件结构

```
hermes-ha/
├── scripts/
│   ├── ha_sync.py        # 核心同步脚本（所有 HA 操作）
│   ├── ha_watchdog.sh    # Pi 端看门狗（cron 每分钟）
│   ├── ha_notify.sh      # 通知脚本
│   ├── config_merge.py   # 配置叠加合并工具
│   └── gen_shared.py     # 生成共享配置
├── references/
│   └── pi-deploy.sh      # Pi 端一键部署脚本
├── SKILL.md              # 完整技术文档（含所有场景、 pitfalls）
└── README.md             # 本文件
```

### 环境要求

- Python 3.10+（主节点）
- Bash 4+（备节点）
- SSH 免密登录（ed25519）
- systemd 用户服务（备节点网关管理）

### 所有覆盖场景

| 场景 | 行为 |
|------|------|
| 主节点在线，备节点在线 | 主节点运行，每 30min 推送数据，每 2min 心跳 |
| 主节点离线，备节点在线 | 心跳超时后备节点自动提升，启动网关（最多 3min 延迟） |
| 两个节点同时在线但都有网关 | 备节点看门狗检测到心跳 → 停止备网关 |
| 网络分区（脑裂） | 接管时双向合并数据，epoch 竞争防止双主 |

### 数据同步策略

| 数据类型 | 策略 |
|----------|------|
| SQLite 数据库 | 行级合并（INSERT OR IGNORE） |
| 文本文件 | 最新修改时间胜出 |
| 目录（skills/sessions） | rsync 增量同步（仅添加） |
| 通道数据 | 仅主节点推送 |

### 详细文档

完整技术文档（含部署步骤、所有 pitfalls、测试流程）见 [SKILL.md](SKILL.md)。

### License

MIT

---

## English

Hot-standby high availability system for [Hermes Agent](https://github.com/hermes-agent/hermes-agent), providing automatic failover and data synchronization between two nodes.

### Architecture

```
  ┌──────────────────┐         ┌──────────────────┐
  │   Primary (WSL)   │ ──SSH── │  Standby (Raspberry Pi) │
  │   x86_64, fast    │  ────→  │  arm64, always-on       │
  └──────────────────┘  one-way └──────────────────┘
```

- **Primary**: Auto-takes over when online, higher-performance x86 device (e.g., WSL)
- **Standby**: Always-on low-power device (e.g., Raspberry Pi), auto-promotes on primary failure
- **One-way sync**: All data sync initiated by primary (standby usually behind NAT)
- **Data safety**: Bidirectional merge, never overwrite-and-lose

### Core Features

| Feature | Description |
|---------|-------------|
| Auto Failover | Standby auto-promotes on heartbeat timeout (default 180s) |
| Lossless Data Sync | SQLite row-level merge, text files latest-mtime-wins, incremental rsync for dirs |
| Config Sync | Auto-sync core config via `config.shared.yaml`, preserve environment-specific settings |
| Split-Brain Prevention | Epoch counter + role competition mechanism prevents dual-primary |
| Event Logging | JSONL event log for all failover/takeover/handoff events |
| Node Awareness | Auto-detect node type (WSL/Pi), logs and state include node identity |

### Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/gegewu81/hermes-ha.git

# 2. Set environment variables
export HA_PI_HOST=<your-pi-ip>
export HA_PI_USER=<your-pi-username>

# 3. Set up SSH key auth
ssh-keygen -t ed25519 -C 'hermes-ha@wsl' -f ~/.ssh/id_ed25519 -N ''
ssh-copy-id <YOUR_PI_USER>@<YOUR_PI_IP>

# 4. Initialize node
python3 scripts/ha_sync.py init-node

# 5. Check status
python3 scripts/ha_sync.py status
```

### File Structure

```
hermes-ha/
├── scripts/
│   ├── ha_sync.py        # Core sync script (all HA operations)
│   ├── ha_watchdog.sh    # Pi watchdog (cron every minute)
│   ├── ha_notify.sh      # Notification script
│   ├── config_merge.py   # Config overlay merge tool
│   └── gen_shared.py     # Generate shared config
├── references/
│   └── pi-deploy.sh      # Pi one-click deployment script
├── SKILL.md              # Full technical documentation (all scenarios, pitfalls)
└── README.md             # This file
```

### Requirements

- Python 3.10+ (primary node)
- Bash 4+ (standby node)
- SSH passwordless login (ed25519)
- systemd user services (standby gateway management)

### All Scenarios Covered

| Scenario | Behavior |
|----------|----------|
| Primary online, standby online | Primary runs, pushes data every 30min, heartbeat every 2min |
| Primary offline, standby online | Standby auto-promotes on heartbeat timeout, starts gateway (max 3min delay) |
| Both online with dual gateway | Standby watchdog detects heartbeat → stops standby gateway |
| Network partition (split-brain) | Takeover merges both sides, epoch competition prevents dual-primary |

### Data Sync Strategy

| Data Type | Strategy |
|-----------|----------|
| SQLite databases | Row-level merge (INSERT OR IGNORE) |
| Text files | Latest mtime wins |
| Directories (skills/sessions) | Incremental rsync (add-only) |
| Channel data | Primary-only push |

### Full Documentation

Complete technical documentation (deployment steps, all pitfalls, testing procedures) available in [SKILL.md](SKILL.md).

### License

MIT
