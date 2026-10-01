#!/usr/bin/env python3
"""Persistent TCP relay: listen on 127.0.0.1:11700, forward each connection
to <FRPS_HOST>:<FRPS_PORT> (frps) via HTTP CONNECT through the egress proxy.
Lets frpc reach frps even though this VM has no direct outbound TCP."""
import base64
import os
import socket
import threading
from urllib.parse import urlparse

LISTEN_HOST, LISTEN_PORT = "127.0.0.1", 11700
TARGET_HOST, TARGET_PORT = "<FRPS_HOST>", <FRPS_PORT>
PROXY_HOST, PROXY_PORT = "198.19.0.1", 3128

pw = urlparse(os.environ["https_proxy"]).password
AUTH = base64.b64encode(f"hatch-runtime:{pw}".encode()).decode()


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client):
    try:
        up = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=20)
        up.sendall(
            f"CONNECT {TARGET_HOST}:{TARGET_PORT} HTTP/1.1\r\n"
            f"Host: {TARGET_HOST}:{TARGET_PORT}\r\n"
            f"Proxy-Authorization: Basic {AUTH}\r\n\r\n".encode()
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = up.recv(4096)
            if not chunk:
                client.close()
                return
            resp += chunk
        if b"200" not in resp.split(b"\r\n", 1)[0]:
            client.close()
            return
        threading.Thread(target=pipe, args=(client, up), daemon=True).start()
        pipe(up, client)
    except OSError:
        try:
            client.close()
        except OSError:
            pass


srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((LISTEN_HOST, LISTEN_PORT))
srv.listen(32)
print(f"frp-relay listening on {LISTEN_HOST}:{LISTEN_PORT} -> {TARGET_HOST}:{TARGET_PORT}", flush=True)
while True:
    conn, _ = srv.accept()
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
