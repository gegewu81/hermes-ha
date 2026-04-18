#!/usr/bin/env python3
"""
config_merge.py — Merge config.shared.yaml into config.yaml
Usage:
  python3 config_merge.py [--dry-run] [--check]

Strategy:
  - config.shared.yaml contains core behavioral sections (memory, mcp, tts, skills, etc.)
  - config.yaml may have environment-specific overrides (security, command_allowlist, etc.)
  - This script overlays shared sections onto config.yaml, preserving env-specific ones.
  - Called by ha_sync.py during push/takeover/handoff.

Shared sections (always synced from shared -> config.yaml):
  memory, plugins, skills, cron, agent, browser, auxiliary, compression,
  model, tts, stt, voice, streaming, terminal, logging, display,
  session_reset, smart_model_routing, delegation, platform_toolsets

Environment-specific sections (preserved, never overwritten):
  command_allowlist, security
"""

import yaml
import sys
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
SHARED_FILE = HERMES_HOME / "config.shared.yaml"
CONFIG_FILE = HERMES_HOME / "config.yaml"

# Sections that come from shared config (authoritative)
SHARED_SECTIONS = [
    # Core agent behavior
    "agent",
    "browser",
    # Model & auxiliary
    "model",
    "auxiliary",
    "compression",
    # Memory & plugins
    "memory",
    "plugins",
    # Skills & cron
    "skills",
    "cron",
    # Voice
    "tts",
    "stt",
    "voice",
    # Terminal & display
    "terminal",
    "display",
    "streaming",
    "logging",
    # Session management
    "session_reset",
    "smart_model_routing",
    "delegation",
    "platform_toolsets",
    # Channel configs (structure sync, not credentials)
    "discord",
    "telegram",
    "slack",
    "mattermost",
    "whatsapp",
    # Other shared
    "context",
    "checkpoints",
    "code_execution",
    "network",
    "web",
    "privacy",
    "toolsets",
    "fallback_providers",
    "custom_providers",
    "human_delay",
    "personalities",
    "quick_commands",
    "honcho",
    "providers",
    "credential_pool_strategies",
    "group_sessions_per_user",
    "file_read_max_chars",
    "timezone",
    "prefill_messages_file",
    "approvals",
]

# Sections that are environment-specific (never overwritten by shared)
ENV_SECTIONS = [
    "command_allowlist",
    "security",
    "mcp_servers",  # contains env-specific keys/URLs
]

# Sections managed by hermes internally (never touch)
INTERNAL_SECTIONS = [
    "_config_version",
    "dashboard",
    "bedrock",
]


def merge_config(dry_run=False, check_only=False):
    """Merge config.shared.yaml into config.yaml."""

    if not SHARED_FILE.exists():
        print("  config.shared.yaml not found, skipping config merge")
        return False

    if not CONFIG_FILE.exists():
        print("  config.yaml not found, skipping config merge")
        return False

    # Load both
    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f) or {}
    with open(SHARED_FILE) as f:
        shared = yaml.safe_load(f) or {}

    if not shared:
        print("  config.shared.yaml is empty, skipping")
        return False

    if check_only:
        # Just report what would change
        changes = []
        for section in SHARED_SECTIONS:
            if section in shared:
                if section not in config:
                    changes.append("  [NEW] %s" % section)
                elif config[section] != shared[section]:
                    changes.append("  [DIFF] %s" % section)
        if changes:
            print("  Config merge would apply %d changes:" % len(changes))
            for c in changes:
                print(c)
        else:
            print("  Config is already in sync with shared")
        return len(changes) > 0

    # Apply shared sections (only non-empty ones)
    changes = 0
    for section in SHARED_SECTIONS:
        if section in shared:
            shared_val = shared[section]
            # Skip empty sections — don't overwrite real data with empty dicts
            if isinstance(shared_val, (dict, list)) and len(shared_val) == 0:
                continue
            if section not in config or config[section] != shared[section]:
                config[section] = shared[section]
                changes += 1

    if changes == 0:
        if not dry_run:
            print("  config.yaml already matches shared, no changes")
        return False

    if dry_run:
        print("  Would apply %d section updates (dry-run)" % changes)
        return True

    # Write back with original ordering style
    # Reorder: internal sections first, then shared, then env-specific
    ordered = {}
    for k in INTERNAL_SECTIONS:
        if k in config:
            ordered[k] = config[k]
    for k in SHARED_SECTIONS:
        if k in config:
            ordered[k] = config[k]
    for k in ENV_SECTIONS:
        if k in config:
            ordered[k] = config[k]
    # Any remaining keys
    for k in config:
        if k not in ordered:
            ordered[k] = config[k]

    with open(CONFIG_FILE, "w") as f:
        yaml.dump(ordered, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print("  config.yaml merged %d sections from shared" % changes)
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    merge_config(dry_run=args.dry_run, check_only=args.check)
