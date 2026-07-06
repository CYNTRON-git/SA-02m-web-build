#!/usr/bin/env python3
"""Scan COM4 and optionally start flash to verify job stays active."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_common import FLASHER_SOCK, WEB_COOKIE, connect_ssh  # noqa: E402

AUTH = f"-H 'Cookie: {WEB_COOKIE}'"
FW_FILE = "MR-02m_1.0.9.1.fw"


def run(client, cmd, timeout=180):
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
    code, out, _ = run(client, cmd, timeout=120)
    if code != 0:
        raise RuntimeError(out)
    lines = out.rstrip("\n").split("\n")
    return int(lines[-1]), "\n".join(lines[:-1])


def wait_job(client, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        http, body = curl(client, "GET", f"/jobs/{job_id}")
        if http != 200:
            time.sleep(1)
            continue
        snap = json.loads(body)
        st = snap.get("state")
        if st in ("done", "cancelled", "error"):
            return snap
        time.sleep(2)
    raise TimeoutError(job_id)


def main():
    c = connect_ssh()

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
    print("scan:", http, body[:120])
    if http != 200:
        return 1
    job_id = json.loads(body)["job_id"]
    snap = wait_job(c, job_id, timeout=180)
    devices = snap.get("devices") or []
    print(f"scan done: {len(devices)} devices")
    for d in devices:
        print(f"  addr={d.get('address')} sig={d.get('signature')} ver={d.get('version')}")

    targets = [d for d in devices if int(d.get("address") or 0) in (6, 8)]
    if not targets:
        print("No modules at addr 6/8 — scan-only test OK")
        c.close()
        return 0

    targets_payload = []
    for d in targets:
        targets_payload.append(
            {
                "address": d.get("address"),
                "serial": d.get("serial"),
                "signature": d.get("signature"),
                "baudrate": d.get("baudrate"),
                "parity": d.get("parity"),
                "stopbits": d.get("stopbits"),
                "in_bootloader": d.get("in_bootloader"),
                "version": d.get("version"),
            }
        )

    http, body = curl(
        c,
        "POST",
        "/flash_batch",
        {
            "port": "COM4",
            "firmware_channel": "upload",
            "firmware_file": FW_FILE,
            "use_fast_modbus": False,
            "devices_on_port": devices,
            "targets": targets_payload,
        },
    )
    print("flash:", http, body[:120])
    if http != 200:
        c.close()
        return 1
    flash_id = json.loads(body)["job_id"]

    time.sleep(5)
    http, st_body = curl(c, "GET", "/status")
    st = json.loads(st_body)
    print(f"status during flash: busy={st.get('busy')} irreversible={st.get('active_jobs')}")

    http, rel = curl(c, "POST", "/ports/release", {"port": "COM4"})
    print(f"release during flash: HTTP {http}")

    http, cancel_body = curl(c, "POST", "/cancel", {"job_id": flash_id})
    print(f"cancel (early): HTTP {http} {cancel_body[:80]}")

    snap2 = wait_job(c, flash_id, timeout=600)
    print(
        f"flash finished: state={snap2.get('state')} progress={snap2.get('progress')} "
        f"irreversible={snap2.get('irreversible')}"
    )
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
