#!/usr/bin/env python3
"""Sequential async service control test with restore."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_common import HOST, WEB_COOKIE  # noqa: E402

BASE = f"http://{HOST}:9999"
SAFE = ["mqtt-telemetry", "mosquitto", "mqtt-bridge", "mplc4", "codesys", "docker"]
TIMEOUTS = {"codesys": 180, "docker": 120}


def http(method, path, data=None, timeout=30):
    headers = {"Cookie": WEB_COOKIE}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, time.time() - t0, json.loads(resp.read().decode())


def wants_start(s):
    if s.get("masked") or s.get("user_disabled"):
        return True
    active = s.get("active", "")
    return active not in ("active", "running", "activating")


def get_list():
    _, _, j = http("GET", "/cgi-bin/services_ctrl.cgi")
    return {s["id"]: s for s in j.get("services", [])}


def poll_until(sid, want_start, max_s):
    deadline = time.time() + max_s
    while time.time() < deadline:
        time.sleep(2)
        s = get_list().get(sid)
        if s and wants_start(s) != want_start:
            return round(time.time() - (deadline - max_s), 1), s
    return None, get_list().get(sid)


def test_service(sid, orig):
    orig_want = wants_start(orig)
    action = "stop" if not orig_want else "start"
    row = {"id": sid, "action": action, "orig": {"active": orig.get("active"), "user_disabled": orig.get("user_disabled")}}
    try:
        st, post_s, j = http("POST", "/cgi-bin/services_ctrl.cgi", {"id": sid, "action": action})
    except urllib.error.HTTPError as e:
        row["fail"] = f"HTTP {e.code}"
        return row
    row["post_http"] = st
    row["post_sec"] = round(post_s, 3)
    row["pending"] = j.get("pending")
    if st != 200 or not j.get("ok") or not j.get("pending"):
        row["fail"] = "bad_post_response"
        return row
    poll_s, final = poll_until(sid, action == "start", TIMEOUTS.get(sid, 90))
    row["poll_sec"] = poll_s
    row["state_ok"] = poll_s is not None
    if final:
        row["final"] = {"active": final.get("active"), "user_disabled": final.get("user_disabled")}
    restore = "start" if orig_want else "stop"
    if restore != action:
        try:
            st2, _, j2 = http("POST", "/cgi-bin/services_ctrl.cgi", {"id": sid, "action": restore})
            row["restore_pending"] = j2.get("pending")
            if st2 == 200 and j2.get("pending"):
                rp, _ = poll_until(sid, restore == "start", TIMEOUTS.get(sid, 120))
                row["restore_poll_sec"] = rp
                row["restore_ok"] = rp is not None
        except Exception as e:
            row["restore_error"] = str(e)
    return row


def main():
    initial = get_list()
    print("Initial services:", sorted(initial.keys()))
    results = []
    for sid in SAFE:
        if sid not in initial:
            results.append({"id": sid, "skip": "not_listed"})
            continue
        print(f"Testing {sid}...")
        results.append(test_service(sid, initial[sid]))
        time.sleep(1)
    print("\n=== TEST MATRIX ===")
    for r in results:
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
