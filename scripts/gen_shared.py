import yaml
from pathlib import Path

SHARED_SECTIONS = [
    "agent", "browser",
    "model", "auxiliary", "compression",
    "memory", "plugins",
    "skills", "cron",
    "tts", "stt", "voice",
    "terminal", "display", "streaming", "logging",
    "session_reset", "smart_model_routing", "delegation", "platform_toolsets",
    "discord", "telegram", "slack", "mattermost", "whatsapp",
    "context", "checkpoints", "code_execution",
    "network", "web", "privacy",
    "toolsets", "fallback_providers", "custom_providers",
    "human_delay", "personalities", "quick_commands",
    "honcho", "providers", "credential_pool_strategies",
    "group_sessions_per_user", "file_read_max_chars", "timezone",
    "prefill_messages_file", "approvals",
]

config_path = Path.home() / ".hermes" / "config.yaml"
shared_path = Path.home() / ".hermes" / "config.shared.yaml"

with open(config_path) as f:
    config = yaml.safe_load(f)

shared = {}
for section in SHARED_SECTIONS:
    if section in config:
        shared[section] = config[section]

header = (
    "# config.shared.yaml - HA shared behavioral config\n"
    "# Synced between WSL and Pi by ha_sync.py\n"
    "# WSL (primary) is authoritative.\n"
    "# Environment-specific sections NOT in this file:\n"
    "#   command_allowlist, security, mcp_servers, _config_version, dashboard, bedrock\n"
    "#\n"
)

with open(shared_path, "w") as f:
    f.write(header)
    ordered_keys = sorted(shared.keys(), key=lambda x: SHARED_SECTIONS.index(x) if x in SHARED_SECTIONS else 999)
    for k in ordered_keys:
        f.write("# === %s ===\n" % k)
        f.write(yaml.dump({k: shared[k]}, default_flow_style=False, allow_unicode=True))
        f.write("\n")

print("Generated config.shared.yaml with %d sections:" % len(shared))
for k in ordered_keys:
    val = shared[k]
    if isinstance(val, dict):
        print("  %s: %d keys" % (k, len(val)))
    elif isinstance(val, list):
        print("  %s: %d items" % (k, len(val)))
    else:
        print("  %s: %s" % (k, str(val)[:40]))
