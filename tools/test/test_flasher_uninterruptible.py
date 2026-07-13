#!/usr/bin/env python3
"""Verify flasher uninterruptible API and guards on SA-02m."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_common import FLASHER_SOCK, REPO_ROOT, WEB_COOKIE, connect_ssh  # noqa: E402

FW_REMOTE = "/var/lib/sa02m-flasher/firmware/MR-02m_1.0.9.1.fw"


def run(client, cmd: str, timeout: float = 120.0) -> tuple[int, str, str]:
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out, err


def flasher_curl(client, method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    auth = f"-H 'Cookie: {WEB_COOKIE}'"
    if body is None:
        cmd = (
            f"curl -sS --unix-socket {FLASHER_SOCK} -X {method} "
            f"{auth} http://localhost{path} -w '\\n%{{http_code}}'"
        )
    else:
        js = json.dumps(body, ensure_ascii=False).replace("'", "'\\''")
        cmd = (
            f"curl -sS --unix-socket {FLASHER_SOCK} -X {method} "
            f"{auth} -H 'Content-Type: application/json' "
            f"-d '{js}' http://localhost{path} -w '\\n%{{http_code}}'"
        )
    code, out, err = run(client, cmd, timeout=60)
    if code != 0:
        raise RuntimeError(err or out)
    lines = out.rstrip("\n").split("\n")
    if len(lines) < 2:
        return 0, out
    return int(lines[-1]), "\n".join(lines[:-1])


def main() -> int:
    client = connect_ssh()
    ok = True

    fw_env = os.environ.get("SA02M_FW")
    fw_local = Path(fw_env) if fw_env else REPO_ROOT / "firmware" / "MR-02m_1.0.9.1.fw"
    if fw_local.is_file():
        sftp = client.open_sftp()
        sftp.put(str(fw_local), FW_REMOTE)
        sftp.chmod(FW_REMOTE, 0o644)
        sftp.close()
        run(client, f"chown sa02m-flasher:sa02m-flasher {FW_REMOTE}")
        print(f"uploaded firmware -> {FW_REMOTE}")
    else:
        print(f"WARN: firmware not found (set SA02M_FW or place at {fw_local})")

    run(
        client,
        f"curl -sS --unix-socket {FLASHER_SOCK} -X POST "
        f"-H 'Cookie: {WEB_COOKIE}' -H 'Content-Type: application/json' "
        "-d '{\"download\":false}' http://localhost/firmware/refresh >/dev/null || true",
    )

    st_code, st_body = flasher_curl(client, "GET", "/status")
    print(f"/status HTTP {st_code}: {st_body[:200]}")
    if st_code != 200:
        ok = False
    else:
        st = json.loads(st_body)
        if "busy" not in st or "active_jobs" not in st:
            print("FAIL: /status missing fields")
            ok = False

    rel_code, _ = flasher_curl(client, "POST", "/ports/release", {"port": "COM4"})
    print(f"release (idle) HTTP {rel_code}")
    if rel_code not in (200, 409):
        ok = False

    scan_code, scan_body = flasher_curl(
        client,
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
    print(f"scan HTTP {scan_code}: {scan_body[:120]}")
    if scan_code != 200:
        print("FAIL: scan did not start")
        client.close()
        return 1
    job_id = json.loads(scan_body).get("job_id")
    if not job_id:
        print("FAIL: no job_id")
        client.close()
        return 1

    time.sleep(1.5)
    _, st_body2 = flasher_curl(client, "GET", "/status")
    st2 = json.loads(st_body2)
    print(f"/status during scan: busy={st2.get('busy')} jobs={len(st2.get('active_jobs') or [])}")

    rel_code2, rel_body2 = flasher_curl(client, "POST", "/ports/release", {"port": "COM1"})
    print(f"release during scan HTTP {rel_code2}: {rel_body2[:120]}")
    if rel_code2 != 409:
        print("FAIL: expected 409 on release during active job")
        ok = False

    _, svc_out, _ = run(client, "/usr/local/sbin/sa02m-web-service-ctl.sh list")
    svc = json.loads(svc_out)
    if "flasher_busy" not in svc:
        print("FAIL: service list missing flasher_busy")
        ok = False
    else:
        print(f"svc list flasher_busy={svc.get('flasher_busy')}")

    code3, out3, _ = run(client, "/usr/local/sbin/sa02m-web-service-ctl.sh start mplc4")
    print(f"start mplc4 during scan: exit={code3} body={out3.strip()[:120]}")
    if '"error":"flasher_busy"' not in out3:
        print("FAIL: expected flasher_busy when starting mplc4 during scan")
        ok = False

    flasher_curl(client, "POST", "/cancel", {"job_id": job_id})
    print("cancelled scan job")

    for _ in range(30):
        _, st_body = flasher_curl(client, "GET", "/status")
        if not json.loads(st_body).get("busy"):
            break
        time.sleep(1)

    client.close()
    if ok:
        print("ALL CHECKS PASSED")
        return 0
    print("SOME CHECKS FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
