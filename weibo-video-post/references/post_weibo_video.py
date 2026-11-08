#!/usr/bin/env python3
"""Post one video Weibo from a cloud Linux container using a saved session.

Prerequisite: a logged-in session from the weibo-qr-login skill
(``$WB_LOGIN_DIR/state.json`` + profile). This script never logs in and never
touches the monitor's own browser/process: it opens a fresh browser context
from ``state.json``.

Single attempt, never reposts: on failure it writes ``post_video_result.json``
with ``ok=false`` and a reason, then exits. The caller decides whether a
retry is safe (check the profile first -- a blind retry can double-post).

Key lesson, learned the hard way: after ``set_input_files`` the publish
button ("发送") becomes enabled almost immediately, LONG before the video
upload/transcode finishes. Clicking it then silently drops the post -- no
error, nothing appears on the profile. Do NOT gate on the button state.
Gate on the upload actually finishing: the video cover thumbnail appears
(``sinaimg`` image count rises above baseline) AND no "上传中" text is
visible, AND at least 15 s have passed since the file was set. Only then
click send. A 5 s clip needed ~10 s of upload + ~40 s after send before it
showed on the profile; verify by polling the profile, not by the click.

Privacy: this script reads only the local session file and the video you
pass in. It prints no cookie, token or credential -- only progress lines and
the final post URL. ``state.json`` and logs stay on your own machine and must
never be committed to a repository or pasted into a public channel.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone


BASE = os.path.expanduser(os.environ.get("WB_LOGIN_DIR", "~/weibo-login"))
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium")
USER_AGENT = os.environ.get(
    "WB_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
PROXY = os.environ.get("WB_PROXY", "")  # e.g. http://127.0.0.1:3128; empty = no proxy
VIDEO = os.environ.get("WB_VIDEO", "")
CAPTION = os.environ.get("WB_CAPTION", "")
UID = os.environ.get("WB_UID", "")  # your own Weibo numeric user id
RESULT = os.environ.get("WB_RESULT", os.path.join(BASE, "post_video_result.json"))

MIN_UPLOAD_SETTLE_SEC = 15   # never click send sooner than this after set_input_files
UPLOAD_TIMEOUT_SEC = 180     # max wait for cover + no "上传中"
VERIFY_ATTEMPTS = 6          # profile polls after send (~2 min total)


class FlowError(Exception):
    pass


def log(msg):
    print("[" + time.strftime("%H:%M:%S") + "] " + str(msg), flush=True)


def cst_now():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def sinaimg_count(page):
    try:
        return page.locator('img[src*="sinaimg"]').count()
    except Exception:
        return -1


def run():
    if not VIDEO or not os.path.isfile(VIDEO):
        raise FlowError("WB_VIDEO not set or file missing: %s" % VIDEO)
    if not CAPTION:
        raise FlowError("WB_CAPTION not set")
    if not UID:
        raise FlowError("WB_UID not set (your own Weibo numeric user id)")
    if not os.path.isfile(os.path.join(BASE, "state.json")):
        raise FlowError("no saved session at %s -- run weibo-qr-login first"
                        % os.path.join(BASE, "state.json"))

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        args = ["--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"]
        if PROXY:
            args.append("--proxy-server=" + PROXY)
        kwargs = dict(executable_path=CHROMIUM_PATH, args=args)
        browser = None
        try:
            try:
                browser = p.chromium.launch(headless=False, **kwargs)
                log("launched headed chromium")
            except Exception as e:
                log("headed launch failed (%s), trying headless" % e)
                browser = p.chromium.launch(headless=True, **kwargs)
            ctx = browser.new_context(
                storage_state=os.path.join(BASE, "state.json"),
                viewport={"width": 1280, "height": 900}, user_agent=USER_AGENT)
            page = ctx.new_page()
            page.goto("https://weibo.com/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(8000)
            log("loaded title=" + repr(page.title()) + " caption-chars=" + str(len(CAPTION)))
            if page.get_by_role("button", name="登录/注册").count() > 0:
                page.screenshot(path=os.path.join(BASE, "post-video-fail.jpg"),
                                type="jpeg", quality=60)
                raise FlowError("not logged in (login button present) -- re-login first")
            # Duplicate guard: abort if the caption's first line is already on the profile.
            try:
                page.goto("https://weibo.com/u/" + UID, wait_until="domcontentloaded",
                          timeout=60000)
                page.wait_for_timeout(5000)
                if page.get_by_text(CAPTION.split("\n")[0], exact=False).count() > 0:
                    raise FlowError("caption already posted (duplicate guard)")
                page.goto("https://weibo.com/", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                log("pre-check: video not posted yet, composing")
            except FlowError:
                raise
            except Exception as e:
                log("pre-check err (continuing): %s" % e)
            try:
                page.locator('[placeholder*="新鲜事"]').first.click(timeout=10000)
                log("clicked composer")
            except Exception as e:
                raise FlowError("composer click failed: %s" % e)
            page.wait_for_timeout(2000)
            try:
                ta = page.locator('textarea[placeholder*="新鲜事"]')
                if ta.count() == 0:
                    ta = page.locator("textarea:visible")
                log("editor textareas: %d" % ta.count())
                ta.first.click(timeout=5000)
                ta.first.fill(CAPTION)
                log("filled caption")
            except Exception as e:
                raise FlowError("editor fill failed: %s" % e)
            baseline = sinaimg_count(page)
            log("sinaimg baseline: %d" % baseline)
            try:
                inputs = page.locator('input[type="file"]')
                log("file inputs: %d" % inputs.count())
                if inputs.count() == 0:
                    raise RuntimeError("no file input in editor")
                inputs.first.set_input_files(VIDEO)
                log("set video file")
            except Exception as e:
                raise FlowError("video input failed: %s" % e)
            # Upload wait: cover present AND no "上传中" AND minimum settle time.
            # The send button's enabled state is NOT a signal -- it flips on
            # immediately after file selection, before the upload finishes.
            t0 = time.time()
            ready = False
            for i in range(UPLOAD_TIMEOUT_SEC // 5):
                cover = sinaimg_count(page) > baseline
                try:
                    uploading = page.get_by_text("上传中", exact=False).count() > 0
                except Exception:
                    uploading = False
                elapsed = time.time() - t0
                if cover and not uploading and elapsed >= MIN_UPLOAD_SETTLE_SEC:
                    ready = True
                    log("upload done after %ds (cover present, no 上传中)" % int(elapsed))
                    break
                if i % 6 == 0:
                    log("waiting upload... %ds, sinaimg=%d, uploading=%s"
                        % (int(elapsed), sinaimg_count(page), uploading))
                page.wait_for_timeout(5000)
            if not ready:
                page.screenshot(path=os.path.join(BASE, "post-video-fail.jpg"),
                                type="jpeg", quality=60)
                raise FlowError("video upload did not finish in %ds" % UPLOAD_TIMEOUT_SEC)
            page.wait_for_timeout(5000)
            log("publishing")
            try:
                for name in ["完成", "确定"]:
                    btn = page.get_by_role("button", name=name, exact=True)
                    if btn.count() > 0 and btn.first.is_visible():
                        btn.first.click(timeout=3000)
                        log("clicked dialog button " + name)
                        page.wait_for_timeout(1000)
            except Exception as e:
                log("dialog dismiss err: %s" % e)
            try:
                # The publish button is named 发送, not 发布.
                page.get_by_role("button", name="发送", exact=True).first.click(timeout=10000)
                log("clicked send")
            except Exception as e:
                raise FlowError("send click failed: %s" % e)
            page.wait_for_timeout(5000)
            # Verify on the profile: video transcode takes a while after send.
            first_line = CAPTION.split("\n")[0]
            found = 0
            link = ""
            for attempt in range(VERIFY_ATTEMPTS):
                page.goto("https://weibo.com/u/" + UID, wait_until="domcontentloaded",
                          timeout=60000)
                page.wait_for_timeout(8000)
                found = page.get_by_text(first_line, exact=False).count()
                log("profile check %d: caption matches %d" % (attempt + 1, found))
                if found > 0:
                    break
                page.wait_for_timeout(15000)
            if found <= 0:
                page.screenshot(path=os.path.join(BASE, "post-video-unverified.jpg"),
                                type="jpeg", quality=60)
                raise FlowError("send clicked but video post not found on profile page")
            try:
                for sel in ['a[href*="/detail/"]', 'a[href*="/' + UID + '/"]']:
                    a = page.locator(sel).first
                    if a.count() > 0:
                        link = a.get_attribute("href") or ""
                        if link:
                            break
            except Exception as e:
                log("link extract err: %s" % e)
            if link and not link.startswith("http"):
                link = "https://weibo.com" + link
            return {"ok": True, "url": link, "time_cst": cst_now(),
                    "note": "video post found on profile page"}
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


try:
    result = run()
except FlowError as e:
    result = {"ok": False, "reason": str(e)}
except Exception as e:
    result = {"ok": False, "reason": "unexpected: %s" % e}
with open(RESULT, "w") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
