#!/usr/bin/env python3
"""Generic free-model probe for OpenAI-compatible inference APIs.

Three layers:
  1. Dynamic discovery: GET <API_BASE>/v1/models, filter IDs ending with FREE_SUFFIX
  2. Pricing check: pricing.prompt == 0 and pricing.completion == 0
  3. Real inference: POST <API_BASE>/v1/chat/completions with a minimal prompt

Output contract (for cron/scheduler integration):
  "OK: ..."    -> everything fine, stay silent
  "ALERT: ..." -> default model broken or zero working models

Configuration: edit the CONFIG section below, or set env vars.
State file tracks consecutive failures and the last working set.
"""
import json
import os
import subprocess
import sys

# ---------------------------------------------------------------- CONFIG ---
API_BASE = os.environ.get("PROBE_API_BASE", "https://<API_HOST>/v1")
# Suffix that marks free-tier models in the catalog, e.g. ":free"
FREE_SUFFIX = os.environ.get("PROBE_FREE_SUFFIX", ":free")
# The model your workload depends on; alert if it stops working.
DEFAULT_MODEL = os.environ.get("PROBE_DEFAULT_MODEL", "<org>/<model>:free")
# Where to persist state between runs.
STATE_PATH = os.environ.get(
    "PROBE_STATE_PATH",
    os.path.expanduser("~/.free-model-probe-state.json"),
)
# ----------------------------------------------------------------------------


def get_token():
    """Return a Bearer token. Replace with your provider's auth method."""
    token = os.environ.get("PROBE_API_TOKEN")
    if not token:
        raise RuntimeError(
            "no token: set PROBE_API_TOKEN env var or override get_token()"
        )
    return token


def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {"consecutive_failures": 0, "last_alert": None}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def api_get(token, path):
    out = subprocess.run(
        ["curl", "-sS", "-m", "40", "-H", f"Authorization: Bearer {token}",
         f"{API_BASE}{path}"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"curl failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def is_free(entry):
    pricing = (entry or {}).get("pricing") or {}
    try:
        return float(pricing.get("prompt", "0")) == 0 and float(
            pricing.get("completion", "0")) == 0
    except (TypeError, ValueError):
        return False


def probe_inference(token, model_id):
    """Minimal chat completion. Returns None on success, error string otherwise."""
    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    })
    out = subprocess.run(
        ["curl", "-sS", "-m", "40", "-X", "POST",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Content-Type: application/json",
         "-d", payload, f"{API_BASE}/chat/completions"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        return f"curl failed: {out.stderr.strip()[:120]}"
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return f"non-JSON response: {out.stdout[:120]}"
    if data.get("error") or "choices" not in data:
        return f"inference error: {json.dumps(data.get('error') or data)[:160]}"
    return None


def main():
    state = load_state()
    try:
        token = get_token()
        models = {m["id"]: m for m in api_get(token, "/models").get("data", [])}
    except Exception as e:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        save_state(state)
        if state["consecutive_failures"] >= 2:
            print(f"ALERT: model probe failed {state['consecutive_failures']}x "
                  f"in a row: {type(e).__name__}: {e}")
            return 1
        print(f"OK (transient failure #{state['consecutive_failures']}): "
              f"{type(e).__name__}")
        return 0

    free_ids = sorted(mid for mid in models if mid.endswith(FREE_SUFFIX))

    problems, working, broken = [], [], []
    for mid in free_ids:
        if not is_free(models.get(mid)):
            broken.append(f"{mid} (pricing != 0)")
            continue
        # Listing is not enough -- probe actual serving.
        err = probe_inference(token, mid)
        if err:
            broken.append(f"{mid} ({err[:80]})")
        else:
            working.append(mid)

    if DEFAULT_MODEL not in working:
        problems.append(f"default model {DEFAULT_MODEL} not working; "
                        f"working: {', '.join(working) or 'none'}")
    if not working:
        problems.append("no working free models at all")

    prev = state.get("last_working") or []
    changes = []
    added = sorted(set(working) - set(prev))
    removed = sorted(set(prev) - set(working))
    if added:
        changes.append(f"new: {', '.join(added)}")
    if removed:
        changes.append(f"lost: {', '.join(removed)}")

    state["consecutive_failures"] = 0
    state["last_working"] = working
    if problems:
        state["last_alert"] = "; ".join(problems)
        save_state(state)
        print("ALERT: " + "; ".join(problems))
        return 1

    state["last_alert"] = None
    save_state(state)
    msg = f"OK: {len(working)} free models working ({', '.join(working)})"
    if changes:
        msg += " | changes: " + "; ".join(changes)
    if broken:
        msg += f" | broken catalog entries: {'; '.join(broken)}"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
