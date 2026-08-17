#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SA-02m stand FEL watcher: detect 1f3a:efe8 → sunxi-fel uboot → netboot.

Idempotent: one flash session per FEL appearance; cooldown after success/fail.

Usage (WSL/Linux):
  python3 fel-agent.py [--env stand.env]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STAND_DIR = Path(__file__).resolve().parent
IMAGING_DIR = STAND_DIR.parent
DEFAULT_ENV = STAND_DIR / "stand.env"
VID, PID = 0x1F3A, 0xEFE8


def load_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip()
    return data


def status_post(base: str, **fields) -> None:
    url = base.rstrip("/") + "/api/status"
    body = urllib.parse.urlencode({k: v for k, v in fields.items() if v is not None}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[fel-agent] status update failed: {exc}", flush=True)


def fel_present_lsusb(vid: int, pid: int) -> bool:
    try:
        out = subprocess.check_output(["lsusb"], text=True, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    tag = f"{vid:04x}:{pid:04x}"
    return tag.lower() in out.lower()


def fel_present_sysfs(vid: int, pid: int) -> bool:
    base = Path("/sys/bus/usb/devices")
    if not base.is_dir():
        return False
    for d in base.iterdir():
        try:
            idv = (d / "idVendor").read_text().strip()
            idp = (d / "idProduct").read_text().strip()
        except OSError:
            continue
        if idv.lower() == f"{vid:04x}" and idp.lower() == f"{pid:04x}":
            return True
    return False


def fel_present(vid: int, pid: int) -> bool:
    return fel_present_sysfs(vid, pid) or fel_present_lsusb(vid, pid)


def find_sunxi_fel() -> str:
    for cand in ("sunxi-fel", "/usr/local/bin/sunxi-fel", "/usr/bin/sunxi-fel"):
        try:
            subprocess.check_call(
                [cand, "-v"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return cand
        except (FileNotFoundError, subprocess.CalledProcessError):
            # -v may return non-zero on some builds; still accept if executable
            if Path(cand).is_file() or _which(cand):
                return cand
    raise SystemExit("FATAL: sunxi-fel not found (sudo apt install sunxi-tools)")


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)


def run_fel_uboot(sunxi: str, uboot: Path, script: Path | None) -> int:
    cmd = [sunxi, "uboot", str(uboot)]
    if script and script.is_file():
        cmd.append(str(script))
    print(f"[fel-agent] exec: {' '.join(cmd)}", flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.stdout:
        print(p.stdout, end="", flush=True)
    if p.stderr:
        print(p.stderr, end="", file=sys.stderr, flush=True)
    return p.returncode


def write_session(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--cooldown", type=float, default=45.0)
    args = ap.parse_args()

    env = load_env(args.env if args.env.is_file() else STAND_DIR / "stand.env.example")
    vid = int(env.get("FEL_VID", f"{VID:04x}"), 16)
    pid = int(env.get("FEL_PID", f"{PID:04x}"), 16)
    status_port = int(env.get("STATUS_PORT", "8765"))
    status_base = f"http://127.0.0.1:{status_port}"
    tftp = Path(env.get("TFTP_ROOT", str(IMAGING_DIR / "stand-data" / "tftpboot")))
    if not tftp.is_absolute():
        tftp = IMAGING_DIR / tftp
    uboot = tftp / "u-boot-sunxi-with-spl.bin"
    if not uboot.is_file():
        uboot = IMAGING_DIR / "boot" / "u-boot-sunxi-with-spl.bin"
    script = tftp / "fel-boot.scr"
    if not script.is_file():
        script = tftp / "boot.scr"
    session_path = IMAGING_DIR / "stand-data" / "run" / "fel-session.json"
    log_dir = IMAGING_DIR / "stand-data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sunxi = find_sunxi_fel()
    print(
        f"[fel-agent] watching FEL {vid:04x}:{pid:04x}  uboot={uboot}  script={script}",
        flush=True,
    )

    armed = True
    last_gone = time.time()

    while True:
        present = fel_present(vid, pid)
        if not present:
            if not armed and (time.time() - last_gone) >= args.cooldown:
                armed = True
                print("[fel-agent] re-armed (cooldown elapsed)", flush=True)
            time.sleep(args.poll)
            continue

        if not armed:
            time.sleep(args.poll)
            continue

        # New FEL session
        session_id = time.strftime("%Y%m%d-%H%M%S")
        print(f"[fel-agent] FEL seen session={session_id}", flush=True)
        status_post(
            status_base,
            state="FEL_SEEN",
            progress=1,
            message="FEL device detected",
            session_id=session_id,
        )
        write_session(
            session_path,
            {"session_id": session_id, "t": time.time(), "phase": "fel"},
        )

        if not uboot.is_file():
            status_post(
                status_base,
                state="FAIL",
                progress=0,
                message=f"missing uboot {uboot}",
                session_id=session_id,
            )
            armed = False
            last_gone = time.time()
            time.sleep(args.poll)
            continue

        status_post(
            status_base,
            state="NETBOOT",
            progress=5,
            message="sunxi-fel uboot + script",
            session_id=session_id,
        )
        rc = run_fel_uboot(sunxi, uboot, script if script.is_file() else None)
        if rc != 0:
            status_post(
                status_base,
                state="FAIL",
                progress=0,
                message=f"sunxi-fel failed rc={rc}",
                session_id=session_id,
            )
            armed = False
            last_gone = time.time()
            # wait until FEL disappears before re-arm logic
            while fel_present(vid, pid):
                time.sleep(args.poll)
            last_gone = time.time()
            continue

        status_post(
            status_base,
            state="FLASHING",
            progress=10,
            message="U-Boot loaded — waiting netinstall",
            session_id=session_id,
        )
        write_session(
            session_path,
            {
                "session_id": session_id,
                "t": time.time(),
                "phase": "netboot_started",
            },
        )
        armed = False
        # Wait for FEL gadget to drop (U-Boot took over)
        deadline = time.time() + 30
        while fel_present(vid, pid) and time.time() < deadline:
            time.sleep(0.5)
        last_gone = time.time()
        print("[fel-agent] session handed to netinstall / postflash-monitor", flush=True)
        time.sleep(args.poll)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[fel-agent] stop", flush=True)
        raise SystemExit(0)
