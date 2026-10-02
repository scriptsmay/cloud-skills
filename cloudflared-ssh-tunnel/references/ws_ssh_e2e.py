#!/usr/bin/env python3
"""E2E: websocket upgrade to https://<PUBLIC_HOSTNAME>/ via the egress proxy,
then read the SSH banner from the origin through the tunnel.
Expect: HTTP/1.1 101 + banner starting with 'SSH-2.0-'.

Usage:
    HTTPS_PROXY="http://<EGRESS_PROXY_HOST:PORT>" python3 ws_ssh_e2e.py <PUBLIC_HOSTNAME>
"""
import base64, os, socket, ssl, sys
from urllib.parse import urlparse

if len(sys.argv) < 2 or sys.argv[1].startswith("<"):
    sys.exit("usage: ws_ssh_e2e.py <PUBLIC_HOSTNAME>")
HOSTNAME = sys.argv[1]

def via_proxy_connect(target_host, target_port, timeout=20):
    u = urlparse(os.environ["HTTPS_PROXY"])  # no-auth per design
    s = socket.create_connection((u.hostname, u.port or 3128), timeout=timeout)
    s.sendall(f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
              f"Host: {target_host}:{target_port}\r\n\r\n".encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk: raise RuntimeError("proxy closed during CONNECT")
        resp += chunk
    if b" 200" not in resp.split(b"\r\n", 1)[0]:
        raise RuntimeError(f"CONNECT rejected: {resp[:80]!r}")
    return s

raw = via_proxy_connect(HOSTNAME, 443)
tls = ssl.create_default_context().wrap_socket(raw, server_hostname=HOSTNAME)
key = base64.b64encode(os.urandom(16)).decode()
tls.sendall(f"GET / HTTP/1.1\r\nHost: {HOSTNAME}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
resp = b""
while b"\r\n\r\n" not in resp:
    resp += tls.recv(4096)
status = resp.split(b"\r\n", 1)[0].decode(errors="replace")
print("HTTP status:", status)
assert "101" in status, f"expected 101, got: {resp[:200]!r}"
# 101 之后 edge 把 origin 的 TCP 流用 websocket frame 包裹回来，解析一帧
tls.settimeout(10)
hdr = tls.recv(2)
ln = hdr[1] & 0x7F
if ln == 126: ln = int.from_bytes(tls.recv(2), "big")
elif ln == 127: ln = int.from_bytes(tls.recv(8), "big")
payload = b""
while len(payload) < ln:
    payload += tls.recv(ln - len(payload))
print("origin banner:", payload.decode(errors="replace").strip())
assert payload.startswith(b"SSH-2.0-"), "not an SSH banner"
print("E2E OK: edge -> tunnel -> sshd")
