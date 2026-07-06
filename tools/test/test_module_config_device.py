#!/usr/bin/env python3
"""Test module config open flow on device."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_common import WEB_COOKIE, connect_ssh  # noqa: E402

DEV6 = {
    "address": 6,
    "baudrate": 115200,
    "parity": "N",
    "stopbits": 1,
    "signature": "6AO6AI",
    "app_version": "1.0.9.1",
    "bootloader_version": "0.0.0.9",
    "serial": 235566384,
    "in_bootloader": False,
    "supports_fast_modbus": True,
}


def run(c, cmd, timeout=90):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    return (o.read() + e.read()).decode("utf-8", errors="replace")


def api(c, path, body=None):
    if body is None:
        raw = run(c, f"curl -s -b '{WEB_COOKIE}' 'http://127.0.0.1:9999/api/flasher{path}'")
    else:
        data = json.dumps(body, ensure_ascii=False)
        cmd = (
            f"curl -s -b '{WEB_COOKIE}' -H 'Content-Type: application/json' "
            f"-d @- 'http://127.0.0.1:9999/api/flasher{path}'"
        )
        _, o, e = c.exec_command(cmd, timeout=120)
        o.channel.send(data)
        o.channel.shutdown_write()
        raw = (o.read() + e.read()).decode("utf-8", errors="replace")
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    print("RAW:", raw[:800])
    return None


def main():
    c = connect_ssh()

    print("=== before ===")
    print(run(c, "systemctl is-active sa02m-modbus-mqtt; sudo fuser -v /dev/COM4 2>&1 | head -5"))

    ports = api(c, "/ports")
    com4 = next((p for p in ports.get("ports", []) if p.get("key") == "COM4"), {})
    print("COM4 ports:", json.dumps(com4, ensure_ascii=False))

    rel = api(c, "/ports/release", {"port": "COM4"})
    print("release:", json.dumps(rel, ensure_ascii=False)[:1200])

    print("=== after release ===")
    print(run(c, "systemctl is-active sa02m-modbus-mqtt; sudo fuser -v /dev/COM4 2>&1 | head -5"))

    snap_body = {
        "port": "COM4",
        "device": DEV6,
        "snapshot_detail": "full",
        "active_tab": "info",
    }
    print("snapshot request...")
    snap = api(c, "/device_config/snapshot", snap_body)
    if snap:
        print("snapshot ok:", snap.get("ok"), snap.get("kind"), snap.get("info", {}).get("signature"))
    else:
        print("snapshot FAILED")

    rst = api(c, "/ports/restore", {"port": "COM4"})
    print("restore:", json.dumps(rst, ensure_ascii=False)[:800])
    c.close()


if __name__ == "__main__":
    main()
