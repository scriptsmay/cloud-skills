#!/usr/bin/env python3
"""Send a Telegram DM via your Telegram bot. Usage: tg-notify.py "message".

Reads the bot token from ~/.hermes/.env (600) and the recipient user IDs from the
pairing approved list. Never prints secrets. Uses httpx with the egress
proxy (TELEGRAM_PROXY) because the adapter needs it explicitly.
"""
import json
import os
import re
import sys

for _v in ("no_proxy", "NO_PROXY"):
    os.environ.pop(_v, None)

import httpx

ENV_FILE = os.path.expanduser("~/.hermes/.env")
APPROVED = os.path.expanduser("~/.hermes/platforms/pairing/telegram-approved.json")


def read_token():
    for line in open(ENV_FILE):
        m = re.match(r"^TELEGRAM_BOT_TOKEN=(.+)$", line.strip())
        if m:
            return m.group(1)
    raise RuntimeError("TELEGRAM_BOT_TOKEN not found in .env")


def read_proxy():
    for line in open(ENV_FILE):
        m = re.match(r"^TELEGRAM_PROXY=(.+)$", line.strip())
        if m:
            return m.group(1)
    return None


def read_user_ids():
    d = json.load(open(APPROVED))
    ids = [k for k in d.keys() if k.isdigit()]
    if not ids:
        raise RuntimeError("no approved telegram users")
    return ids


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("message", help="text message")
    ap.add_argument("--photo", default=None, help="image file to send instead/as well")
    args = ap.parse_args()
    msg = args.message
    token = read_token()
    proxy = read_proxy()
    user_ids = read_user_ids()

    # httpx needs the proxy CA for the egress MITM
    verify = os.environ.get("SSL_CERT_FILE", True)
    ok = 0
    client_kw = {"verify": verify, "timeout": 60}
    if proxy:
        client_kw["proxy"] = proxy
    with httpx.Client(**client_kw) as c:
        for uid in user_ids:
            try:
                if args.photo:
                    with open(args.photo, "rb") as f:
                        files = {"photo": (os.path.basename(args.photo), f, "image/png")}
                        data = {"chat_id": uid, "caption": msg[:1024]}
                        r = c.post(
                            f"https://api.telegram.org/bot{token}/sendPhoto",
                            data=data, files=files,
                        )
                else:
                    r = c.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": uid, "text": msg},
                    )
                if r.status_code == 200 and r.json().get("ok"):
                    ok += 1
                else:
                    print(f"send to {uid} failed: {r.text[:120]}", file=sys.stderr)
            except Exception as e:
                print(f"send to {uid} error: {e}", file=sys.stderr)
    print(f"sent to {ok}/{len(user_ids)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
