#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run etc/sa02m-check-service-perms.sh on SA-02m via SSH."""
from __future__ import annotations

import argparse
import pathlib
import sys

try:
    import paramiko
except ImportError as exc:
    print("paramiko required: pip install paramiko", file=sys.stderr)
    raise SystemExit(2) from exc

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "etc" / "sa02m-check-service-perms.sh"
REMOTE = "/tmp/sa02m-check-service-perms.sh"

DEFAULT_HOST = "192.168.1.136"
DEFAULT_USER = "root"
DEFAULT_PASS = "cyntron"


def main() -> int:
    ap = argparse.ArgumentParser(description="SA-02m service data directory permission audit")
    ap.add_argument("--fix", action="store_true", help="Apply permission fixes on device")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PASS)
    args = ap.parse_args()

    if not SCRIPT.is_file():
        print(f"missing {SCRIPT}", file=sys.stderr)
        return 2

    mode = "--fix" if args.fix else "audit"
    body = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        args.host,
        username=args.user,
        password=args.password,
        timeout=20,
        allow_agent=False,
        look_for_keys=False,
    )
    sftp = client.open_sftp()
    with sftp.file(REMOTE, "w") as f:
        f.write(body)
    sftp.chmod(REMOTE, 0o755)
    sftp.close()

    cmd = f"bash {REMOTE} {mode}"
    _, stdout, stderr = client.exec_command(cmd, timeout=120)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    client.close()

    sys.stdout.buffer.write(out.encode("utf-8", errors="replace"))
    if err:
        sys.stderr.buffer.write(err.encode("utf-8", errors="replace"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
