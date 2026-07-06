#!/usr/bin/env python3
"""Test AI sensor write on COM4 addr6 with MQTT bridge active + panel poll race."""
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
    "serial": 235566384,
    "in_bootloader": False,
    "supports_fast_modbus": True,
}


def run(c, cmd, timeout=60):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    return (o.read() + e.read()).decode("utf-8", errors="replace")


def api(c, path, body):
    p = json.dumps(body, ensure_ascii=False).replace("'", "'\"'\"'")
    raw = run(
        c,
        f"curl -s -b '{WEB_COOKIE}' -H 'Content-Type: application/json' "
        f"-d '{p}' 'http://127.0.0.1:9999/api/flasher{path}'",
    )
    for line in raw.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    return {"raw": raw[:500]}


def ai1_sensor(snap):
    ai = (((snap.get("mr") or {}).get("ai") or {}).get("channels") or [])
    return ai[0].get("sensor_code") if ai else None


def main():
    c = connect_ssh()

    print("=== release COM4 ===")
    print(api(c, "/ports/release", {"port": "COM4"}))

    print("\n=== start mqtt-bridge (empty devices) ===")
    print(run(c, "sudo /usr/local/sbin/sa02m-web-service-ctl.sh start mqtt-bridge"))
    time.sleep(2)
    print(run(c, "systemctl is-active sa02m-modbus-mqtt-bridge 2>/dev/null || echo inactive"))

    print("\n=== baseline snapshot ===")
    snap = api(
        c,
        "/device_config/snapshot",
        {
            "port": "COM4",
            "device": DEV6,
            "snapshot_detail": "full",
            "active_tab": "ai_1",
        },
    )
    cur = ai1_sensor(snap)
    print("AI1 sensor_code:", cur)
    if cur is None:
        print("FAIL: no AI channel", snap.get("error") or snap)
        c.close()
        return 1

    targets = [3, 4, 7, 3]
    ok = True
    for new in targets:
        if new == cur:
            continue
        print(f"\n=== write reg 400 = {new} (was {cur}) with bridge active ===")
        wr = api(c, "/device_config/holding", {"port": "COM4", "device": DEV6, "reg": 400, "value": new})
        if wr.get("error"):
            print("WRITE ERR:", wr)
            ok = False
            break
        got = ai1_sensor(wr)
        print("immediate snapshot sensor_code:", got)
        if got != new:
            print("FAIL: write response mismatch")
            ok = False
            break

        print("=== panel poll race (3x) ===")
        for _ in range(3):
            panel = api(
                c,
                "/device_config/snapshot",
                {
                    "port": "COM4",
                    "device": DEV6,
                    "snapshot_detail": "panel",
                    "active_tab": "ai_1",
                },
            )
            pc = ai1_sensor(panel)
            print("  panel sensor_code:", pc)
            if pc != new:
                print("FAIL: panel poll shows stale sensor_code")
                ok = False
        cur = new

    print("\n=== final full snapshot ===")
    final = api(
        c,
        "/device_config/snapshot",
        {"port": "COM4", "device": DEV6, "snapshot_detail": "full", "active_tab": "ai_1"},
    )
    fc = ai1_sensor(final)
    print("final sensor_code:", fc, "expected:", cur)
    if fc != cur:
        ok = False

    print("\n=== flasher.js deployed ===")
    print(run(c, "grep -c _aiSensorPending /var/www/network_config/static/js/flasher.js"))

    c.close()
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
