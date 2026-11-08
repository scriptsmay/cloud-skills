#!/usr/bin/env python3
"""Weibo QR-code login on a cloud Linux container.

Logs in once via QR scan and exits. There is deliberately no login-state
detection or monitoring here: after the scan succeeds the script saves the
persistent profile + state.json, writes a final status.json, takes a
logged-in screenshot for the account owner to eyeball, and exits.

Keeping the session alive afterwards (heartbeats, container suspend/resume,
noticing a logout, re-login after expiry) is the operator's own business
and is not part of this skill.

Privacy: everything this script writes (QR screenshots, profile dir,
status.json, state.json) stays in WB_LOGIN_DIR on YOUR OWN machine. QR
screenshots are single-use login credentials -- send one only to the account
owner over a private channel, delete it after the login, and never commit any
of these files to a shared repository.

Usage:
    python3 -m venv venv && ./venv/bin/python -m pip install playwright
    WB_LOGIN_DIR=$HOME/weibo-login CHROMIUM_PATH=/usr/bin/chromium \
        ./venv/bin/python -P weibo_qr_login.py
    # -P (safe_path) matters: a file named inspect.py in the working
    # directory shadows the stdlib `inspect` module and breaks any script
    # that imports asyncio. Do not name your own files inspect.py.
"""
import json
import os
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("WB_LOGIN_DIR", os.path.expanduser("~/weibo-login"))
PROFILE = os.path.join(BASE, "profile")
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium")
SCAN_TIMEOUT = int(os.environ.get("WB_SCAN_TIMEOUT_SEC", "1800"))
# Update Chrome/<major> to match your installed Chromium when it drifts.
USER_AGENT = os.environ.get(
    "WB_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

os.makedirs(BASE, exist_ok=True)


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print("[" + ts + "] " + str(msg), flush=True)


def write_status(d):
    d["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(BASE, "status.json"), "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def shoot(page, name):
    # JPEG, not PNG: a full-page PNG base64-encoded exceeds most exec/output
    # limits and gets truncated mid-transfer. JPEG quality 60 is enough.
    page.screenshot(path=os.path.join(BASE, name), type="jpeg", quality=60)
    log("saved " + name)


def grab_qr(page):
    """Screenshot the QR code img in the login popup. Returns True if saved."""
    for sel in ["img[src*='qr.weibo.cn']", "img[src*='qrcode']", "img[src*='qr']",
                "[class*='qr-code'] img", "[class*='qrcode'] img"]:
        try:
            els = page.query_selector_all(sel)
            for el in els:
                box = el.bounding_box()
                # Square-size check: the feed also contains small images whose
                # URLs mention "qr"; only a ~square 80-400px image is the code.
                if (box and 80 < box["width"] < 400 and 80 < box["height"] < 400
                        and abs(box["width"] - box["height"]) < 40):
                    el.screenshot(path=os.path.join(BASE, "qr.jpg"), type="jpeg", quality=90)
                    ts = time.strftime("%H%M%S")
                    # Timestamped filename: re-sending the same path gets
                    # served from the client cache as the OLD (expired) code.
                    el.screenshot(path=os.path.join(BASE, "qr-" + ts + ".jpg"),
                                  type="jpeg", quality=90)
                    log("saved qr.jpg + qr-" + ts + ".jpg from " + sel + " box=" + str(box))
                    return True
        except Exception as e:
            log("selector " + sel + " err: " + str(e))
    return False


def ensure_fresh_qr(page):
    """If the popup shows an expired QR, click refresh, then screenshot it."""
    try:
        refresh = page.get_by_text("点击刷新", exact=False).first
        if refresh and refresh.is_visible():
            refresh.click(timeout=5000)
            log("clicked QR refresh (expired code)")
            page.wait_for_timeout(4000)
    except Exception as e:
        log("qr refresh click err: " + str(e))
        try:
            els = page.query_selector_all("img[src*='qr.weibo.cn']")
            if els:
                box = els[0].bounding_box()
                if box:
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    log("mouse-clicked QR area to refresh")
                    page.wait_for_timeout(4000)
        except Exception as e2:
            log("qr area click err: " + str(e2))
    return grab_qr(page)


def confirm_login(page):
    """One-time check that the single scan succeeded.

    This poll exists only to know when the scan has worked; the script does
    not keep checking afterwards and records no identity.

    Two dead ends, do not re-try them:
    - In-page fetch("https://m.weibo.cn/api/config") is blocked cross-origin
      (TypeError: Failed to fetch), so it always reports logged-out.
    - text='有什么新鲜事' never matches: that string is a placeholder
      attribute of the composer input, not text content.

    Logged-in homepage: no '登录/注册' button, and the composer input carries
    a placeholder containing '新鲜事'. Logged-out: the button exists.
    Note: "button not found" is itself the logged-in signal -- do not treat
    a missing login button as an error and retry forever.
    """
    try:
        login_btn = page.get_by_role("button", name="登录/注册").count()
        composer = page.locator('[placeholder*="新鲜事"]').count()
        return login_btn == 0 and composer > 0
    except Exception as e:
        log("confirm_login err: " + str(e))
        return False


def open_login_popup(ctx, page):
    """Click login/register on weibo.com; returns the popup page or None.

    The button does NOT open a modal layer -- it opens a NEW WINDOW
    (passport.weibo.com/sso/signin?...&disp=popup). A plain .click() and the
    openLoginLayer=1 URL parameter both fail to open it; a mouse click on
    the button's center coordinates works, and the first click often does
    nothing, so retry up to 3 times.
    """
    for attempt in range(3):
        try:
            btn = page.get_by_role("button", name="登录/注册").first
            box = btn.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                log("mouse-clicked login/register button (attempt " + str(attempt + 1) + ")")
        except Exception as e:
            log("login button click err: " + str(e))
        for _ in range(10):
            for pg in ctx.pages:
                if "passport.weibo.com/sso/signin" in pg.url:
                    log("popup found: " + pg.url[:100])
                    return pg
            time.sleep(1)
        log("no popup on attempt " + str(attempt + 1) + ", retrying")
        page.wait_for_timeout(3000)
    log("no login popup appeared after 3 attempts")
    return None


def launch_ctx(p):
    """Persistent context: a plain new_context() is ephemeral and the login
    does not survive a restart. The profile dir holds the session cookies.
    Headed first (some flows behave better headed); fall back to headless.
    """
    kwargs = dict(
        user_data_dir=PROFILE,
        executable_path=CHROMIUM_PATH,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 900},
        user_agent=USER_AGENT)
    try:
        ctx = p.chromium.launch_persistent_context(headless=False, **kwargs)
        log("launched persistent headed chromium, profile=" + PROFILE)
        return ctx
    except Exception as e:
        log("headed persistent launch failed (" + str(e) + "), trying headless")
        return p.chromium.launch_persistent_context(headless=True, **kwargs)


with sync_playwright() as p:
    ctx = launch_ctx(p)
    page = ctx.new_page()
    page.goto("https://weibo.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(10000)
    log("main page loaded title=" + repr(page.title()))
    logged_in = confirm_login(page)
    if logged_in:
        log("already logged in on load")
    else:
        popup = open_login_popup(ctx, page)
        if popup:
            popup.wait_for_timeout(5000)
            # Force a brand-new QR session: reload the login popup once.
            popup.reload(wait_until="domcontentloaded")
            popup.wait_for_timeout(5000)
            log("reloaded popup for a fresh QR session")
            popup.screenshot(path=os.path.join(BASE, "popup.jpg"), type="jpeg", quality=60)
            log("saved popup.jpg")
            qr_found = ensure_fresh_qr(popup)
        else:
            qr_found = False
        write_status({"stage": "waiting_scan", "qr_found": qr_found,
                      "url": page.url, "title": page.title()})
        log("QR saved to " + BASE + " -- send qr-<timestamp>.jpg to the account "
            "owner over a private channel, then wait for the scan + in-app confirm. "
            "A code lives about 10 minutes; the script refreshes it automatically.")
        deadline = time.time() + SCAN_TIMEOUT
        polls = 0
        while time.time() < deadline:
            polls += 1
            if confirm_login(page):
                logged_in = True
                log("LOGIN CONFIRMED on main page")
                break
            if polls % 12 == 0:
                try:
                    if popup and not popup.is_closed():
                        ensure_fresh_qr(popup)
                        popup.screenshot(path=os.path.join(BASE, "popup.jpg"), type="jpeg", quality=60)
                        log("refreshed QR screenshot (poll " + str(polls) + ")")
                    else:
                        log("popup gone, reopening")
                        popup = open_login_popup(ctx, page)
                except Exception as e:
                    log("qr refresh err: " + str(e))
            page.wait_for_timeout(5000)
    if logged_in:
        page.wait_for_timeout(3000)
        ctx.storage_state(path=os.path.join(BASE, "state.json"))
        shoot(page, "logged-in.jpg")
        write_status({"stage": "logged_in", "url": page.url, "title": page.title()})
        log("logged in, state saved, title=" + repr(page.title()))
        log("done -- exiting. Show logged-in.jpg to the account owner to "
            "confirm the right account; keeping the session alive afterwards "
            "is the operator's own business, not this script's.")
    else:
        write_status({"stage": "scan_timeout", "url": page.url})
        log("scan window expired without login; exiting")
