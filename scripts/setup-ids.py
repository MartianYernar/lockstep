#!/usr/bin/env python3
"""Capture Telegram chat IDs straight from your own bot.

Simpler and less error-prone than the @userinfobot route: whoever messages the
bot shows up here, so you and your father each send /start once and the IDs are
read off the wire rather than copied by hand.

    python3 scripts/setup-ids.py            # watch for 120s
    python3 scripts/setup-ids.py --write    # ...and offer to save into config.yaml
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.yaml")


def token_from_config() -> str:
    if os.environ.get("LOCKSTEP_BOT_TOKEN"):
        return os.environ["LOCKSTEP_BOT_TOKEN"]
    import yaml
    with open(CONFIG) as fh:
        return yaml.safe_load(fh)["telegram"]["bot_token"]


def api(token: str, method: str, **params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=40) as r:
        return json.loads(r.read())


def main() -> int:
    token = token_from_config()
    me = api(token, "getMe")
    if not me.get("ok"):
        print("Bad bot token:", me)
        return 1
    username = me["result"]["username"]
    print(f"Bot: @{username}   https://t.me/{username}")
    print("\nOpen that link and press START. Have your father do the same.")
    print("Watching for 120 seconds...\n")

    seen: dict[int, dict] = {}
    offset = None
    deadline = time.time() + 120
    while time.time() < deadline:
        params = {"timeout": 20}
        if offset is not None:
            params["offset"] = offset
        try:
            res = api(token, "getUpdates", **params)
        except Exception as exc:                              # noqa: BLE001
            print("  (retrying:", exc, ")")
            time.sleep(2)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid and cid not in seen:
                seen[cid] = chat
                name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                uname = f"@{chat['username']}" if chat.get("username") else "(no username)"
                print(f"  ✓ {name or '?':<20} {uname:<20} chat_id = {cid}")
        if len(seen) >= 2:
            print("\n  Two chats seen — that's probably both of you.")
            break

    if not seen:
        print("\nNobody messaged the bot. Make sure you pressed START, not just opened the chat.")
        return 1

    if "--write" in sys.argv:
        ids = list(seen)
        print("\nWhich is which?")
        for i, cid in enumerate(ids):
            print(f"  [{i}] {seen[cid].get('first_name','?')}  {cid}")
        try:
            you = ids[int(input("Your number: ").strip())]
            dad_raw = input("Your father's number (blank to skip): ").strip()
        except (ValueError, IndexError, EOFError):
            print("Skipped.")
            return 0
        import yaml
        with open(CONFIG) as fh:
            cfg = yaml.safe_load(fh)
        cfg["telegram"]["user_chat_id"] = you
        if dad_raw:
            cfg["telegram"]["guardian_chat_id"] = ids[int(dad_raw)]
        with open(CONFIG, "w") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True)
        print(f"\nWrote user_chat_id={you} into config.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
