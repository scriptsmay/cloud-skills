#!/usr/bin/env python3
"""cloudflared edge shim.

Listens on 127.0.0.1:7844. For each inbound connection, opens an HTTP CONNECT
tunnel through the egress proxy ($HTTPS_PROXY) to one of
region1/region2.v2.argotunnel.com:7844 (round-robin), then forwards bytes
bidirectionally. TLS is end-to-end (cloudflared <-> Cloudflare edge);
the shim never sees plaintext.

IMPORTANT: never send Proxy-Authorization. The egress proxy rotates its
password every few minutes, so any stored password is stale almost
immediately (stale password -> 407). The proxy allows unauthenticated
CONNECT to these edge targets (-> 200), so no auth header is sent.
The CONNECT target must be a domain name (the proxy resolves it with its
own unpoisoned DNS); IP literals get rejected/timed out.
"""
import itertools
import os
import selectors
import socket
import threading
import time
from urllib.parse import urlparse

LISTEN = ("127.0.0.1", 7844)
EDGES = [("region1.v2.argotunnel.com", 7844),
         ("region2.v2.argotunnel.com", 7844)]

_edge_cycle = itertools.cycle(EDGES)
_edge_lock = threading.Lock()


def log(*a):
    print("[%s]" % time.strftime("%F %T"), *a, flush=True)


def get_proxy():
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    # strip any userinfo: a stale password would cause 407
    u = urlparse(raw if "://" in raw else "http://" + raw)
    if not u.hostname:
        raise RuntimeError("HTTPS_PROXY not set or unparsable")
    return u.hostname, u.port or 8080


def connect_via_proxy(edge_host, edge_port, timeout=25):
    phost, pport = get_proxy()
    s = socket.create_connection((phost, pport), timeout=timeout)
    s.settimeout(timeout)
    # NOTE: intentionally no Proxy-Authorization header (see module docstring)
    req = ("CONNECT %s:%d HTTP/1.1\r\n"
           "Host: %s:%d\r\n"
           "Proxy-Connection: keep-alive\r\n"
           "\r\n" % (edge_host, edge_port, edge_host, edge_port))
    s.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("proxy closed connection during CONNECT")
        resp += chunk
        if len(resp) > 16384:
            raise RuntimeError("proxy response too large")
    status = resp.split(b"\r\n", 1)[0]
    if b" 200" not in status:
        raise RuntimeError("CONNECT rejected: %r" % status[:120])
    return s


def pipe(a, b):
    sel = selectors.DefaultSelector()
    for src, dst in ((a, b), (b, a)):
        sel.register(src, selectors.EVENT_READ, dst)
    try:
        while True:
            events = sel.select(timeout=180)
            if not events:
                return  # idle too long
            for key, _ in events:
                data = key.fileobj.recv(65536)
                if not data:
                    return
                key.data.sendall(data)
    except (OSError, socket.timeout):
        pass
    finally:
        sel.close()


def handle(client):
    with _edge_lock:
        first = next(_edge_cycle)
    upstream = None
    errs = []
    # try round-robin pick first, then the other edge
    for eh, ep in [first] + [e for e in EDGES if e != first]:
        try:
            upstream = connect_via_proxy(eh, ep)
            first = (eh, ep)
            break
        except Exception as e:  # noqa: BLE001 - want the message for the log
            errs.append("%s:%d: %s" % (eh, ep, e))
    try:
        if upstream is None:
            raise RuntimeError("all edges failed: " + "; ".join(errs))
        try:
            peer = client.getpeername()
        except OSError:
            peer = "?"
        log("linked %s -> %s:%d" % (peer, first[0], first[1]))
        pipe(client, upstream)
    except Exception as e:  # noqa: BLE001
        log("link failed: %s" % e)
    finally:
        for s in (client, upstream):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(128)
    log("shim listening on %s:%d" % LISTEN)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
