#!/bin/bash
# mount-namespace wrapper for cloudflared:
#  - private mount namespace, bind-mount fake /etc/hosts so only cloudflared
#    sees region1/2.v2.argotunnel.com -> 127.0.0.1 (the edge shim)
#  - force TCP transport (sandbox bans UDP, QUIC unusable)
#  - run the Named Tunnel from $TUNNEL_TOKEN (no CLI args needed;
#    ingress config is pushed from the Cloudflare side)
set -euo pipefail

PERSIST_HOME="/home/hatch"
TOKEN_FILE="$PERSIST_HOME/.cloudflared/token"

if [ ! -f "$TOKEN_FILE" ]; then
  echo "missing tunnel token: $TOKEN_FILE" >&2
  exit 1
fi
export TUNNEL_TOKEN="$(cat "$TOKEN_FILE")"
export TUNNEL_TRANSPORT_PROTOCOL=http2

CLOUDFLARED="$(command -v cloudflared)"
[ -n "$CLOUDFLARED" ] || { echo "cloudflared not installed" >&2; exit 1; }

exec unshare --mount --propagation private bash -c '
  mount --bind /etc/cloudflared/hosts.edge /etc/hosts
  exec "$0" tunnel --no-autoupdate --metrics 127.0.0.1:20241 run
' "$CLOUDFLARED"
