#!/usr/bin/env python3
"""Recovery flash for MR-02m modules stuck in bootloader on COM4."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_common import FLASHER_SOCK, WEB_COOKIE, connect_ssh  # noqa: E402

AUTH = f"-H 'Cookie: {WEB_COOKIE}'"
FW = "MR-02m_1.0.9.1.fw"


def run(client, cmd, timeout=600):
    _, o, e = client.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", errors="replace")
    err = e.read().decode("utf-8", errors="replace")
    return o.channel.recv_exit_status(), out, err


def curl(client, method, path, body=None):
    if body is None:
        cmd = f"curl -sS --unix-socket {FLASHER_SOCK} -X {method} {AUTH} http://localhost{path} -w '\\n%{{http_code}}'"
    else:
        js = json.dumps(body, ensure_ascii=False).replace("'", "'\\''")
        cmd = (
            f"curl -sS --unix-socket {FLASHER_SOCK} -X {method} {AUTH} "
            f"-H 'Content-Type: application/json' -d '{js}' http://localhost{path} -w '\\n%{{http_code}}'"
        )
    code, out, err = run(client, cmd, timeout=120)
    if code != 0:
        raise RuntimeError(err or out)
    lines = out.rstrip("\n").split("\n")
    return int(lines[-1]), "\n".join(lines[:-1])


def wait_job(client, job_id, timeout=900):
    deadline = time.time() + timeout
    last_prog = -1
    while time.time() < deadline:
        http, body = curl(client, "GET", f"/jobs/{job_id}")
        if http != 200:
            time.sleep(2)
            continue
        snap = json.loads(body)
        prog = snap.get("progress") or 0
        if prog != last_prog:
            print(f"  progress={prog}% state={snap.get('state')} irreversible={snap.get('irreversible')}")
            last_prog = prog
        st = snap.get("state")
        if st in ("done", "cancelled", "error"):
            return snap
        time.sleep(3)
    raise TimeoutError(job_id)


def main() -> int:
    c = connect_ssh()

    run(
        c,
        f"curl -sS --unix-socket {FLASHER_SOCK} -X POST "
        f"-H 'Cookie: {WEB_COOKIE}' -H 'Content-Type: application/json' "
        "-d '{\"download\":false}' http://localhost/firmware/refresh >/dev/null || true",
    )

    print("Scan COM4…")
    http, body = curl(
        c,
        "POST",
        "/scan",
        {
            "port": "COM4",
            "mode": "fast",
            "baudrates": [115200, 9600],
            "parity": "N",
            "stopbits": 1,
            "addr_min": 1,
            "addr_max": 247,
        },
    )
    if http != 200:
        print("scan failed:", body)
        return 1
    scan_id = json.loads(body)["job_id"]
    snap = wait_job(c, scan_id, timeout=180)
    devices = snap.get("devices") or []
    print(f"Found {len(devices)} device(s):")
    for d in devices:
        print(
            f"  addr={d.get('address')} sig={d.get('signature')} "
            f"bootloader={d.get('in_bootloader')} ver={d.get('app_version')}"
        )

    targets = []
    for d in devices:
        if d.get("in_bootloader") or int(d.get("address") or 0) in (6, 8):
            targets.append(
                {
                    "address": d.get("address"),
                    "serial": d.get("serial"),
                    "signature": d.get("signature"),
                    "baudrate": d.get("baudrate"),
                    "parity": d.get("parity"),
                    "stopbits": d.get("stopbits"),
                    "in_bootloader": bool(d.get("in_bootloader")),
                    "recovery": bool(d.get("in_bootloader")),
                }
            )

    if not targets:
        print("No targets for recovery flash")
        return 1

    print(f"Recovery flash {len(targets)} target(s) with {FW}…")
    http, body = curl(
        c,
        "POST",
        "/flash_batch",
        {
            "port": "COM4",
            "firmware_channel": "local",
            "firmware_file": FW,
            "use_fast_modbus": False,
            "devices_on_port": devices,
            "targets": targets,
            "recovery": True,
        },
    )
    print("flash start:", http, body[:160])
    if http != 200:
        return 1
    flash_id = json.loads(body)["job_id"]
    result = wait_job(c, flash_id, timeout=900)
    print(f"DONE state={result.get('state')} error={result.get('error')}")
    if result.get("state") != "done":
        for e in (result.get("events") or [])[-8:]:
            print(" ", e.get("level"), e.get("message"))
        return 1

    print("Rescan…")
    http, body = curl(
        c,
        "POST",
        "/scan",
        {
            "port": "COM4",
            "mode": "fast",
            "baudrates": [115200, 9600],
            "parity": "N",
            "stopbits": 1,
            "addr_min": 1,
            "addr_max": 247,
        },
    )
    snap2 = wait_job(c, json.loads(body)["job_id"], timeout=180)
    for d in snap2.get("devices") or []:
        print(
            f"  after: addr={d.get('address')} sig={d.get('signature')} "
            f"bootloader={d.get('in_bootloader')} ver={d.get('app_version')}"
        )
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
