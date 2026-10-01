#!/usr/bin/env python3
"""Send a QQ DM via the Hermes QQ bot. Usage: qq-notify.py "message".

Reads bot credentials from ~/.hermes/config.yaml (600) and the recipient's openid from
the pairing approved list. Never prints secrets. Uses httpx (same as the
gateway adapter) because curl gets empty replies on this egress path.
"""
import json
import os
import re
import sys
import time

for _v in ("no_proxy", "NO_PROXY"):
    os.environ.pop(_v, None)

import httpx

CONFIG = os.path.expanduser("~/.hermes/config.yaml")
APPROVED = os.path.expanduser("~/.hermes/platforms/pairing/qqbot-approved.json")
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
UA = "hermes-qq-notify/1.0"


def read_creds():
    cfg = open(CONFIG).read()
    m = re.search(r"qqbot:\s*\n(?:.*\n)*?\s{4}extra:\s*\n((?:\s{6}.*\n)+)", cfg)
    if not m:
        raise RuntimeError("qqbot config not found")
    block = m.group(1)
    app_id = re.search(r"app_id:\s*\"?([^\"]+)\"?", block).group(1).strip()
    secret = re.search(r"client_secret:\s*\"?([^\"]+)\"?", block).group(1).strip()
    return app_id, secret


def read_openid():
    d = json.load(open(APPROVED))
    ids = list(d.keys())
    if not ids:
        raise RuntimeError("no approved qqbot user")
    return ids[0]


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("usage: qq-notify.py \"message\"")
        return 2
    text = sys.argv[1].strip()[:1000]
    app_id, secret = read_creds()
    openid = read_openid()
    with httpx.Client(timeout=30.0, headers={"User-Agent": UA}) as c:
        r = c.post(TOKEN_URL, json={"appId": app_id, "clientSecret": secret})
        r.raise_for_status()
        token = r.json().get("access_token")
        if not token:
            raise RuntimeError("token response missing access_token")
        r = c.post(
            f"{API_BASE}/v2/users/{openid}/messages",
            headers={"Authorization": f"QQBot {token}"},
            json={"content": text, "msg_type": 0,
                  "msg_seq": int(time.time() * 1000) % 100000})
        r.raise_for_status()
        print(f"OK: DM sent (id={r.json().get('id', '?')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
