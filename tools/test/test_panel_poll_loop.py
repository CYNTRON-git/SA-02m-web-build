#!/usr/bin/env python3
"""Rapid panel snapshot test — simulates config modal polling."""
from __future__ import annotations

import json
import sys
import time
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


def run(c, cmd, timeout=60):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    return (o.read() + e.read()).decode("utf-8", errors="replace")


def api(c, path, body):
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
        if s.startswith("{"):
            return json.loads(s)
    return {"raw": raw[:300]}


def main():
    c = connect_ssh()

    print("release:", api(c, "/ports/release", {"port": "COM4"}).get("ok"))
    body = {
        "port": "COM4",
        "device": DEV6,
        "snapshot_detail": "panel",
        "active_tab": "info",
    }
    for i in range(6):
        t0 = time.time()
        r = api(c, "/device_config/snapshot", body)
        dt = time.time() - t0
        ok = r.get("ok")
        mcu = (r.get("mr") or {}).get("mcu") or {}
        print(
            f"poll {i + 1}: ok={ok} dt={dt:.2f}s temp={mcu.get('temp_c')} "
            f"uptime={mcu.get('uptime_str')} err={r.get('error') or r.get('raw', '')[:80]}"
        )
        time.sleep(1)

    api(c, "/ports/restore", {"port": "COM4"})
    c.close()


if __name__ == "__main__":
    main()
