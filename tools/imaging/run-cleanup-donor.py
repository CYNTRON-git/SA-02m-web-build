#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run tools/imaging/cleanup-donor.sh on SA-02m over SSH (Windows/Linux).

Examples:
  py -3 tools/imaging/run-cleanup-donor.py --dry-run --report
  py -3 tools/imaging/run-cleanup-donor.py --apply --report
  py -3 tools/imaging/run-cleanup-donor.py --apply --verbose

Uses the same credentials as tools/ssh/sa02m_remote.py (tools/sa02m-device.env).
Does not touch SSH host keys / machine-id (those are stream-after-cleanup.sh).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError as exc:
    print("paramiko не установлен: pip install paramiko", file=sys.stderr)
    raise SystemExit(2) from exc

REPO = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve().parent / "cleanup-donor.sh"
ENV_FILE = REPO / "tools" / "sa02m-device.env"


def load_env() -> dict[str, str]:
    data: dict[str, str] = {}
    path = Path(os.environ["SA02M_DEVICE_ENV"]) if os.environ.get("SA02M_DEVICE_ENV") else ENV_FILE
    if not path.is_file():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip()
    return data


def connect(host: str, user: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username=user,
        password=password,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
        banner_timeout=60,
    )
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(15)
    return client


def main() -> int:
    env = load_env()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("SA02M_HOST", env.get("SA02M_HOST", "192.168.1.136")))
    ap.add_argument("--user", default=os.environ.get("SA02M_USER", env.get("SA02M_USER", "root")))
    ap.add_argument("--password", default=os.environ.get("SA02M_PASS", env.get("SA02M_PASS", "cyntron")))
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--dry-run", action="store_true", help="только список (default если нет --apply)")
    ap.add_argument("--apply", action="store_true", help="реальное удаление")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--purge-update-state", action="store_true")
    ap.add_argument("--purge-docker", action="store_true")
    ap.add_argument(
        "--purge-installers",
        action="store_true",
        help="также удалить /root/*.deb|*.rpm и /root/mplc_update (по умолчанию сохраняются)",
    )
    args = ap.parse_args()

    if not SCRIPT.is_file():
        print(f"не найден {SCRIPT}", file=sys.stderr)
        return 1

    remote_args: list[str] = []
    if args.apply:
        remote_args.append("--apply")
    else:
        remote_args.append("--dry-run")
    if args.verbose:
        remote_args.append("--verbose")
    if args.report:
        remote_args.append("--report")
    if args.purge_update_state:
        remote_args.append("--purge-update-state")
    if args.purge_docker:
        remote_args.append("--purge-docker")
    if args.purge_installers:
        remote_args.append("--purge-installers")

    cmd = "bash -s -- " + " ".join(remote_args)
    print(f"[run] {args.user}@{args.host}: cleanup-donor.sh {' '.join(remote_args)}", flush=True)

    # Normalize CRLF if script was edited on Windows
    body = SCRIPT.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    client = connect(args.host, args.user, args.password)
    try:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=args.timeout, get_pty=False)
        stdin.write(body)
        stdin.channel.shutdown_write()
        while not stderr.channel.exit_status_ready():
            if stderr.channel.recv_stderr_ready():
                chunk = stderr.channel.recv_stderr(65536)
                if chunk:
                    sys.stderr.buffer.write(chunk)
                    sys.stderr.buffer.flush()
            elif stdout.channel.recv_ready():
                chunk = stdout.channel.recv(65536)
                if chunk:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
            else:
                time.sleep(0.2)
        while stderr.channel.recv_stderr_ready():
            chunk = stderr.channel.recv_stderr(65536)
            if chunk:
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
        out = stdout.read()
        if out:
            sys.stdout.buffer.write(out)
            sys.stdout.buffer.flush()
        return int(stdout.channel.recv_exit_status())
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
