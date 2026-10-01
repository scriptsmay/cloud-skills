#!/bin/bash
# Push heartbeat to Uptime Kuma (monitor: Muse VM, interval 120s)
# Kuma runs on your own server (e.g. a NAS on your tailnet). Reach it via the runtime
# tunnel proxy (port 3130), derived from the egress proxy URL.
# This avoids the egress proxy's flaky CONNECT handling from systemd.

PROXY_ENV="/home/hatch/.frp/proxy.env"
if [ -f "$PROXY_ENV" ]; then
  # shellcheck disable=SC1090
  . "$PROXY_ENV"
fi

# Build tunnel proxy URL: same host/auth as egress proxy, port 3130
TUNNEL_PROXY="${https_proxy%:*}:3130"

curl -s --max-time 25 --proxy "$TUNNEL_PROXY" \
  "<KUMA_BASE_URL>/api/push/<KUMA_PUSH_TOKEN>?status=up&msg=OK&ping=" \
  -o /dev/null -w "%{http_code}\n"
