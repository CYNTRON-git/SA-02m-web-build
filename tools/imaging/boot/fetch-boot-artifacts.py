#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pull SA-02m netboot artifacts from a live donor into tools/imaging/boot/.

Usage:
  py -3 tools/imaging/boot/fetch-boot-artifacts.py
  py -3 tools/imaging/boot/fetch-boot-artifacts.py --host 192.168.1.136

Fetches:
  zImage                 (FAT mmcblk2p1)
  sun8i-a40i-sk.dtb      (FAT mmcblk2p1)
  u-boot-sunxi-with-spl.bin  (Armbian package path; optional refresh)

Large blobs (zImage) are gitignored — re-fetch on each stand host.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ssh"))
from sa02m_remote import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PASS,
    DEFAULT_USER,
    connect,
)

BOOT = Path(__file__).resolve().parent
MOUNT = "/tmp/sa02m-bootfat-fetch"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PASS)
    ap.add_argument(
        "--refresh-uboot",
        action="store_true",
        help="Also overwrite u-boot-sunxi-with-spl.bin from donor package",
    )
    args = ap.parse_args()

    BOOT.mkdir(parents=True, exist_ok=True)
    cli = connect(args.host, args.user, args.password)
    try:
        prep = (
            f"mkdir -p {MOUNT} && "
            f"(mountpoint -q {MOUNT} || mount /dev/mmcblk2p1 {MOUNT}) && "
            f"test -r {MOUNT}/zImage && test -r {MOUNT}/sun8i-a40i-sk.dtb"
        )
        _, stdout, stderr = cli.exec_command(prep, timeout=60)
        code = stdout.channel.recv_exit_status()
        if code != 0:
            print(stderr.read().decode("utf-8", errors="replace"), file=sys.stderr)
            print("FATAL: cannot mount FAT boot or missing zImage/DTB", file=sys.stderr)
            return 1

        sftp = cli.open_sftp()
        pairs = [
            (f"{MOUNT}/zImage", BOOT / "zImage"),
            (f"{MOUNT}/sun8i-a40i-sk.dtb", BOOT / "sun8i-a40i-sk.dtb"),
        ]
        if args.refresh_uboot:
            pairs.append(
                (
                    "/usr/lib/linux-u-boot-current-bananapim2ultra/u-boot-sunxi-with-spl.bin",
                    BOOT / "u-boot-sunxi-with-spl.bin",
                )
            )
        for remote, local in pairs:
            print(f"GET {remote} -> {local}")
            sftp.get(remote, str(local))
            print(f"  {local.stat().st_size} bytes")
        sftp.close()
    finally:
        cli.exec_command(f"umount {MOUNT} 2>/dev/null || true")
        cli.close()

    print("OK: boot artifacts ready in", BOOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
