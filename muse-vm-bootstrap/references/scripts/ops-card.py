#!/usr/bin/env python3
"""Generate a mobile-friendly ops status card PNG from status.json.

Usage: ops-card.py [output_path]
Reads ~/workspace/demo-site/ops/status.json, renders a dark cyber-style
vertical card optimized for phone viewing.
"""
import json
import os
import sys
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

STATUS = os.path.expanduser("~/workspace/demo-site/ops/status.json")
W, PAD = 1080, 48
BG = (10, 14, 24)
CARD = (18, 26, 42)
GREEN = (46, 213, 115)
RED = (255, 71, 87)
YELLOW = (255, 184, 0)
CYAN = (0, 210, 255)
WHITE = (235, 240, 250)
GRAY = (130, 145, 170)
LINE = (35, 48, 72)


def font(size):
    for p in [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def fmt_bytes(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"


def fmt_bps(n):
    return fmt_bytes(n) + "/s"


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ops-card.png"
    d = json.load(open(STATUS))
    v = d["vitals"]

    cpu = v["cpu_pct"]
    mem_pct = (1 - v["mem_avail_kb"] / v["mem_total_kb"]) * 100
    disk_pct = (1 - v["disk_free"] / v["disk_total"]) * 100
    up_h = v["uptime_s"] // 3600
    up_m = (v["uptime_s"] % 3600) // 60

    services = d["services"]
    n_ok = sum(1 for s in services if s.get("active"))
    all_ok = n_ok == len(services)

    # Layout: compute rows dynamically
    rows = []
    rows.append(("title", "MUSE OPS", f'{datetime.now().strftime("%m-%d %H:%M")} · uptime {up_h}h{up_m}m'))
    rows.append(("gauges", None, None))
    rows.append(("kv", "负载", f'{v["load_1"]:.2f} / {v["load_5"]:.2f} / {v["load_15"]:.2f}'))
    rows.append(("kv", "网络", f'↑ {fmt_bps(v["net_up_bps"])}  ↓ {fmt_bps(v["net_down_bps"])}'))
    rows.append(("kv", "本月流量", f'↑ {fmt_bytes(v["net_month_up"])}  ↓ {fmt_bytes(v["net_month_rx"])}'))
    rows.append(("section", "服务状态", f"{n_ok}/{len(services)}"))
    for s in services:
        rows.append(("svc", s.get("label") or s["unit"], bool(s.get("active"))))
    rows.append(("section", "链路检测", None))
    links = d["links"]
    rows.append(("svc", "演示站 <YOUR_DOMAIN>", links["demo_site"]["ok"],
                 f'{links["demo_site"]["http"]} · {links["demo_site"]["ms"]}ms'))
    rows.append(("svc", "frp 隧道", links["frp_tunnel"]["ok"]))
    rows.append(("svc", "Kuma 心跳推送", links["kuma_push"]["ok"]))
    rows.append(("section", "AI 模型", None))
    for m in d["models"]["targets"]:
        short = m.split("/")[-1]
        rows.append(("svc", short, True, "FREE"))
    qq = d.get("qq", {})
    rows.append(("section", "QQ 机器人", None))
    rows.append(("svc", "网关", qq.get("gateway_active", False),
                 "READY" if qq.get("gateway_active") else "DOWN"))

    # Measure height
    f_title = font(64)
    f_sec = font(40)
    f_kv = font(36)
    f_svc = font(36)
    h = PAD * 2 + 110
    for row in rows:
        kind = row[0]
        if kind == "title":
            h += 100
        elif kind == "gauges":
            h += 300
        elif kind == "section":
            h += 90
        elif kind == "kv":
            h += 64
        elif kind == "svc":
            h += 72

    img = Image.new("RGB", (W, h), BG)
    dr = ImageDraw.Draw(img)
    y = PAD

    # Header
    dr.text((PAD, y), "MUSE OPS", font=f_title, fill=CYAN)
    ts = rows[0][2]
    dr.text((PAD, y + 72), ts, font=font(32), fill=GRAY)
    y += 150
    dr.line([(PAD, y), (W - PAD, y)], fill=LINE, width=2)
    y += 24

    # Gauges (three side-by-side bars)
    gw = (W - PAD * 2 - 40) // 3
    bx = PAD
    for label, pct in [("CPU", cpu), ("内存", mem_pct), ("磁盘", disk_pct)]:
        val = f"{pct:.0f}%"
        dr.text((bx, y), label, font=font(34), fill=GRAY)
        dr.text((bx + gw - 110, y), val, font=font(34),
               fill=GREEN if pct < 70 else (YELLOW if pct < 90 else RED))
        by = y + 48
        dr.rounded_rectangle([bx, by, bx + gw, by + 22], radius=11, fill=LINE)
        fw = int(gw * min(pct, 100) / 100)
        col = GREEN if pct < 70 else (YELLOW if pct < 90 else RED)
        if fw > 0:
            dr.rounded_rectangle([bx, by, bx + fw, by + 22], radius=11, fill=col)
        bx += gw + 20
    y += 110
    dr.line([(PAD, y), (W - PAD, y)], fill=LINE, width=2)
    y += 24

    for row in rows[2:]:
        kind = row[0]
        a = row[1]
        b = row[2] if len(row) > 2 else None
        extra = row[3] if len(row) > 3 else None
        if kind == "section":
            dr.text((PAD, y), f"◆ {a}", font=f_sec, fill=CYAN)
            if b:
                ok_mark = str(b) == f"{n_ok}/{len(services)}" and all_ok
                col = GREEN if ok_mark else WHITE
                tw = dr.textlength(str(b), font=font(36))
                dr.text((W - PAD - tw, y + 6), str(b), font=font(36), fill=col)
            y += 90
        elif kind == "kv":
            dr.text((PAD, y), a, font=f_kv, fill=GRAY)
            dr.text((PAD + 260, y), b, font=f_kv, fill=WHITE)
            y += 64
        elif kind == "svc":
            dot = GREEN if b else RED
            dr.ellipse([PAD, y + 14, PAD + 28, y + 42], fill=dot)
            dr.text((PAD + 44, y), a, font=f_svc, fill=WHITE)
            if extra:
                tw = dr.textlength(extra, font=font(32))
                dr.text((W - PAD - tw, y + 4), extra, font=font(32), fill=GRAY)
            y += 72
    img.save(out)
    print(f"saved {out} ({W}x{h})")


if __name__ == "__main__":
    main()
