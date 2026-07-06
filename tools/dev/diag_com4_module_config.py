#!/usr/bin/env python3
"""Diagnose module config on COM4 addr 6."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_common import WEB_BASE, WEB_COOKIE, ssh_run  # noqa: E402


def curl_json(url: str, data=None):
    if data is None:
        cmd = f"curl -s -b '{WEB_COOKIE}' '{WEB_BASE}{url}'"
    else:
        payload = json.dumps(data, ensure_ascii=False)
        cmd = (
            f"curl -s -b '{WEB_COOKIE}' -H 'Content-Type: application/json' "
            f"-d '{payload}' '{WEB_BASE}{url}'"
        )
    out = ssh_run(cmd)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            return json.loads(line)
    raise ValueError(f"no json in: {out[:500]}")


def main():
    print("=== Port holders COM4 ===")
    print(ssh_run("fuser -v /dev/COM4 2>&1 || true; lsof /dev/COM4 2>/dev/null || true"))

    print("\n=== Bridge / flasher ===")
    print(
        ssh_run(
            "systemctl is-active sa02m-modbus-mqtt sa02m-flasher 2>/dev/null; "
            "pgrep -af modbus_mqtt_bridge || true"
        )
    )

    print("\n=== MQTT config COM4 ===")
    cfg = curl_json("/cgi-bin/mqtt_config.cgi")
    for d in cfg.get("devices") or []:
        if "COM4" in str(d.get("port", "")):
            print(json.dumps(d, ensure_ascii=False, indent=2)[:2000])

    print("\n=== Flasher scan COM4 ===")
    scan = curl_json(
        "/cgi-bin/flasher_proxy.cgi",
        {
            "path": "/scan",
            "body": {"port": "COM4", "mode": "fast", "baudrates": [115200], "max_addr": 32},
        },
    )
    print(json.dumps(scan, ensure_ascii=False)[:1500])

    dev6 = None
    for d in scan.get("devices") or scan.get("found") or []:
        if int(d.get("address", 0)) == 6:
            dev6 = d
            break
    if not dev6:
        print("addr 6 not found in scan")
        return

    print("\n=== device_config snapshot addr 6 ===")
    snap = curl_json(
        "/cgi-bin/flasher_proxy.cgi",
        {
            "path": "/device_config/snapshot",
            "body": {
                "port": "COM4",
                "device": dev6,
                "snapshot_detail": "full",
                "active_tab": "ai_1",
            },
        },
    )
    err = snap.get("error")
    if err:
        print("ERROR:", err)
        return
    ai = (((snap.get("module") or {}).get("ai") or {}).get("channels") or [])
    print("AI channels:", json.dumps(ai[:3], ensure_ascii=False, indent=2))

    if ai:
        ch1 = ai[0]
        base = ch1.get("register_base", 400)
        cur = ch1.get("sensor_code", 0)
        new = 1 if cur != 1 else 2
        print(f"\n=== Try write sensor reg {base}: {cur} -> {new} ===")
        wr = curl_json(
            "/cgi-bin/flasher_proxy.cgi",
            {
                "path": "/device_config/holding",
                "body": {"port": "COM4", "device": dev6, "reg": base, "value": new},
            },
        )
        print(json.dumps(wr, ensure_ascii=False)[:1200])
        if wr.get("error"):
            print("WRITE FAILED:", wr.get("error"))
        else:
            ai2 = (((wr.get("module") or {}).get("ai") or {}).get("channels") or [])
            if ai2:
                print("after write sensor_code:", ai2[0].get("sensor_code"))


if __name__ == "__main__":
    main()
