#!/usr/bin/env python3
"""Collect infra status -> ~/workspace/demo-site/ops/status.json (atomic write).

No secrets are ever written to the JSON: only service states, vitals and
reachability booleans.
"""
import json
import os
import re
import shutil
import subprocess
import time

OUT = os.path.expanduser("~/workspace/demo-site/ops/status.json")
STATE = os.path.expanduser("~/workspace/ops-collect-state.json")

SERVICES = [
    ("komari-agent", "Komari 监控 Agent"),
    ("kuma-push.timer", "Kuma 心跳推送"),
    ("frp-relay.service", "frp 中继隧道"),
    ("frpc.service", "frpc 客户端"),
    ("demo-site.service", "演示站"),
    ("hermes-gateway.service", "Hermes 网关 (QQ)"),
]

# latency probes: label -> (host, port)
# NOTE: ICMP is blocked and plain TCP is transparently proxied on this VM,
# so only full HTTPS request timing gives honest numbers.


def sh(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", 1


def svc_active(name):
    out, rc = sh(["systemctl", "is-active", name])
    return out == "active"


def cpu_pct():
    """CPU usage % over a 1s sample via /proc/stat."""
    def read():
        with open("/proc/stat") as f:
            p = f.readline().split()
        vals = list(map(int, p[1:]))
        return vals[0] + vals[2], sum(vals)  # (user+nice, total)
    try:
        w1, t1 = read()
        time.sleep(1)
        w2, t2 = read()
        return round((w2 - w1) / max(1, t2 - t1) * 100, 1)
    except Exception:
        return 0


def net_sample():
    """Return (rx_bytes, tx_bytes) summed over non-lo interfaces."""
    rx = tx = 0
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if ":" not in line:
                    continue
                iface, vals = line.split(":", 1)
                iface = iface.strip()
                if iface == "lo":
                    continue
                v = vals.split()
                rx += int(v[0])
                tx += int(v[8])
    except Exception:
        pass
    return rx, tx


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


def main():
    now = int(time.time())
    data = {"updated_at": now, "services": [], "vitals": {}, "links": {},
            "models": [], "qq": {}}

    for unit, label in SERVICES:
        data["services"].append(
            {"unit": unit, "label": label, "active": svc_active(unit)})

    # vitals
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        load = os.getloadavg()
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                if k in ("MemTotal", "MemAvailable"):
                    mem[k] = int(v.split()[0])
        du = shutil.disk_usage("/home/hatch")
        cores = os.cpu_count() or 1

        # network: 1s sample for current speed + persistent totals
        rx1, tx1 = net_sample()
        cpu = cpu_pct()
        rx2, tx2 = net_sample()
        st = load_state()
        month = time.strftime("%Y-%m")
        # handle counter reset (reboot): if current < saved, start over
        tot_rx = st.get("tot_rx", 0)
        tot_tx = st.get("tot_tx", 0)
        if rx2 < st.get("last_rx", 0) or tx2 < st.get("last_tx", 0):
            tot_rx, tot_tx = 0, 0
        else:
            tot_rx += max(0, rx2 - st.get("last_rx", rx2))
            tot_tx += max(0, tx2 - st.get("last_tx", tx2))
        if st.get("month") != month:
            m_rx, m_tx = 0, 0
        else:
            m_rx = st.get("m_rx", 0) + max(0, rx2 - st.get("last_rx", rx2))
            m_tx = st.get("m_tx", 0) + max(0, tx2 - st.get("last_tx", tx2))
        save_state({"tot_rx": tot_rx, "tot_tx": tot_tx,
                    "m_rx": m_rx, "m_tx": m_tx, "month": month,
                    "last_rx": rx2, "last_tx": tx2})

        data["vitals"] = {
            "uptime_s": int(up),
            "cpu_pct": cpu,
            "cpu_cores": cores,
            "load_1": round(load[0], 2),
            "load_5": round(load[1], 2),
            "load_15": round(load[2], 2),
            "mem_total_kb": mem.get("MemTotal", 0),
            "mem_avail_kb": mem.get("MemAvailable", 0),
            "disk_total": du.total,
            "disk_free": du.free,
            "net_up_bps": max(0, tx2 - tx1),
            "net_down_bps": max(0, rx2 - rx1),
            "net_tot_up": tot_tx,
            "net_tot_rx": tot_rx,
            "net_month_up": m_tx,
            "net_month_rx": m_rx,
        }
    except Exception:
        pass

    # latency: honest HTTPS timing only (see note above)
    data["latency"] = []

    # public reachability of the demo domain.
    # NOTE: from systemd, the egress proxy hangs on CONNECT with a hostname
    # (see kuma-push fix 2026-09-29). Use the Cloudflare edge IP directly
    # with a Host header; a 301 (http->https redirect from openresty)
    # proves the full chain: proxy -> Cloudflare -> openresty vhost.
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    env = dict(os.environ)
    for v in ("no_proxy", "NO_PROXY"):
        env.pop(v, None)
    try:
        t0 = time.time()
        r = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "-m", "15", "--header", "Host: <YOUR_DOMAIN>",
             "http://<YOUR_SERVER_IP>/"],
            capture_output=True, text=True, timeout=20, env=env)
        code = r.stdout.strip()
        ms = int((time.time() - t0) * 1000)
        # 301 = openresty vhost alive and redirecting to https; 200 = direct
        ok = code in ("200", "301")
        data["links"]["demo_site"] = {"url": "https://<YOUR_DOMAIN>",
                                      "http": code, "ok": ok, "ms": ms}
    except Exception:
        data["links"]["demo_site"] = {"url": "https://<YOUR_DOMAIN>",
                                      "http": "err", "ok": False, "ms": -1}

    # frpc login health from journal
    out, _ = sh(["journalctl", "-u", "frpc.service", "--since", "10 min ago",
                 "--no-pager", "-q"], timeout=10)
    data["links"]["frp_tunnel"] = {
        "ok": ("login to the server successfully" in out
               or "start proxy success" in out
               or svc_active("frpc.service"))}

    # kuma last push
    out, _ = sh(["journalctl", "-u", "kuma-push.service", "--since", "10 min ago",
                 "--no-pager", "-q"], timeout=10)
    data["links"]["kuma_push"] = {"ok": (" 200" in out or "HTTP/1.1 200" in out
                                         or "success" in out.lower())}

    # nous free-model check state (written by the daily cron script)
    try:
        st_path = os.path.expanduser("~/.hermes/nous_free_models_state.json")
        st = json.load(open(st_path))
        mtime = int(os.path.getmtime(st_path))
        # Working free-model set is discovered dynamically by the check
        # script (models with :free suffix, verified by inference probe).
        # Fall back to the script's TARGETS if the check hasn't run yet.
        targets = list(st.get("last_working") or [])
        if not targets:
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "nous_check",
                    os.path.expanduser("~/workspace/nous-free-models-check.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                targets = list(getattr(mod, "TARGETS", []))
            except Exception:
                pass
        data["models"] = {
            "targets": targets,
            "last_check": mtime,
            "alert": st.get("last_alert"),
            "ok": not st.get("last_alert"),
        }
    except Exception:
        data["models"] = {"targets": [], "last_check": 0, "alert": "no data",
                          "ok": False}

    # qqbot: enabled in hermes config? (boolean only, never the secrets)
    try:
        import re
        cfg = open(os.path.expanduser("~/.hermes/config.yaml")).read()
        m = re.search(r"platforms:\s*\n(?:.*\n)*?\s{2}qqbot:\s*\n"
                      r"(?:.*\n)*?\s{4}enabled:\s*(true|false)", cfg)
        data["qq"] = {"configured": bool(m and m.group(1) == "true"),
                      "gateway_active": svc_active("hermes-gateway.service")}
    except Exception:
        data["qq"] = {"configured": False, "gateway_active": False}

    tmp = OUT + ".tmp"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, OUT)


if __name__ == "__main__":
    main()
