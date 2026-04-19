#!/usr/bin/env python3
"""
HA WeChat direct notification — sends a text message via iLink API,
bypassing the agent entirely. Used by ha_notify.sh and ha_sync.py.

Usage:
    python3 ha_send_weixin.py "message text"
    python3 ha_send_weixin.py --test   # Send test notification
"""

import json
import os
import sys
import ssl
import urllib.request
import hashlib
import time
import random

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
WEIXIN_DIR = os.path.join(HERMES_HOME, "weixin", "accounts")

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (2 << 8) | 0)
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
MSG_TYPE_BOT = 1
MSG_STATE_FINISH = 1
ITEM_TEXT = 1


def _random_wechat_uin():
    """Generate a random WeChat UIN (mimics gateway behavior)."""
    return str(random.randint(1000000000, 9999999999))


def _find_bot_account():
    """Find the first weixin bot account."""
    if not os.path.isdir(WEIXIN_DIR):
        return None, None
    for fname in os.listdir(WEIXIN_DIR):
        if fname.endswith(".bot.json"):
            account_id = fname.replace(".bot.json", "")
            fpath = os.path.join(WEIXIN_DIR, fname)
            return account_id, fpath
    return None, None


def _find_user_id(bot_json_path):
    """Extract user_id (chat target) from bot config."""
    with open(bot_json_path) as f:
        data = json.load(f)
    return data.get("user_id", "")


def _get_context_token(account_id, user_id):
    """Read context token for the user chat."""
    ctx_path = os.path.join(WEIXIN_DIR, f"{account_id}.bot.context-tokens.json")
    if os.path.exists(ctx_path):
        with open(ctx_path) as f:
            tokens = json.load(f)
        return tokens.get(user_id)
    return None


def send_message(text, timeout_s=15):
    """Send a text message directly via iLink API. Returns True on success."""
    account_id, bot_json_path = _find_bot_account()
    if not bot_json_path:
        print("ERROR: No weixin bot account found", file=sys.stderr)
        return False

    with open(bot_json_path) as f:
        bot = json.load(f)

    token = bot["token"]
    base_url = bot.get("base_url", ILINK_BASE_URL)
    to_user = _find_user_id(bot_json_path)
    client_id = account_id.split("@")[0] + "@" + account_id.split("@")[1] if "@" in account_id else account_id

    if not to_user:
        print("ERROR: No user_id in bot config", file=sys.stderr)
        return False

    context_token = _get_context_token(account_id, to_user)

    # Build message payload
    msg = {
        "from_user_id": "",
        "to_user_id": to_user,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token

    body = json.dumps({"msg": msg, "base_info": {"channel_version": CHANNEL_VERSION}})

    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }

    url = f"{base_url.rstrip('/')}/{EP_SEND_MESSAGE}"
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    for k, v in headers.items():
        req.add_header(k, v)

    ctx_ssl = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx_ssl, timeout=timeout_s) as resp:
            result = json.loads(resp.read())
            # Empty {} or ret=0 means success
            ret = result.get("ret")
            errcode = result.get("errcode")
            if (ret is not None and ret not in (0,)) or (errcode is not None and errcode not in (0,)):
                errmsg = result.get("errmsg", result.get("msg", "unknown"))
                print(f"ERROR: iLink API ret={ret} errcode={errcode} errmsg={errmsg}", file=sys.stderr)
                return False
            return True
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: ha_send_weixin.py <message>", file=sys.stderr)
        sys.exit(1)

    arg = sys.argv[1]
    if arg == "--test":
        text = "🔔 HA通知测试：Pi直连微信成功！"
    else:
        text = arg

    success = send_message(text)
    if success:
        print(f"OK: Notification sent: {text[:50]}")
        sys.exit(0)
    else:
        print("FAIL: Notification failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
