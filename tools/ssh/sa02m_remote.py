#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSH к SA-02m для агентов и скриптов (Windows/Linux).

Использование:
  py -3 tools/ssh/sa02m_remote.py exec "systemctl is-active sa02m-flasher"
  py -3 tools/ssh/sa02m_remote.py tail-flasher          # последние 30 строк events.log
  py -3 tools/ssh/sa02m_remote.py watch-flasher         # follow events.log (Ctrl+C)
  py -3 tools/ssh/sa02m_remote.py upload-fw path/to/MR-02m_1.0.9.0.fw
  py -3 tools/ssh/sa02m_remote.py hostkey                # fingerprint для plink -hostkey

Переменные окружения (опционально):
  SA02M_HOST=192.168.1.136
  SA02M_USER=root
  SA02M_PASS=cyntron
  SA02M_HOSTKEY=SHA256:...   # после смены образа обновить (см. docs/AGENTS_SSH_AND_DEVICE_ACCESS.md)
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

DEFAULT_HOST = os.environ.get("SA02M_HOST", "192.168.1.136")
DEFAULT_USER = os.environ.get("SA02M_USER", "root")
DEFAULT_PASS = os.environ.get("SA02M_PASS", "cyntron")
DEFAULT_HOSTKEY = os.environ.get(
    "SA02M_HOSTKEY",
    "SHA256:TMkrSFsuRUe0F1caCEcTNUli9gb7KaQYsPC7FELohKc",
)
FW_CACHE = "/var/lib/sa02m-flasher/firmware"
EVENTS_LOG = "/var/log/sa02m-flasher/events.log"


def connect(host: str, user: str, password: str, timeout: float = 20.0) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=timeout, allow_agent=False, look_for_keys=False)
    return client


def run(client: paramiko.SSHClient, command: str, timeout: float = 120.0) -> tuple[int, str, str]:
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


def cmd_exec(args: argparse.Namespace) -> int:
    client = connect(args.host, args.user, args.password)
    code, out, err = run(client, args.command, timeout=args.timeout)
    client.close()
    if out:
        sys.stdout.buffer.write(out.encode("utf-8", errors="replace"))
    if err:
        sys.stderr.buffer.write(err.encode("utf-8", errors="replace"))
    return code


def cmd_tail_flasher(args: argparse.Namespace) -> int:
    client = connect(args.host, args.user, args.password)
    code, out, err = run(client, f"tail -n {args.lines} {EVENTS_LOG}")
    client.close()
    sys.stdout.buffer.write(out.encode("utf-8", errors="replace"))
    if err:
        sys.stderr.buffer.write(err.encode("utf-8", errors="replace"))
    return code


def cmd_watch_flasher(args: argparse.Namespace) -> int:
    client = connect(args.host, args.user, args.password)
    _, out, _ = run(client, f"wc -l < {EVENTS_LOG}")
    seen = int((out.strip() or "0"))
    print(f"watch {EVENTS_LOG} from line {seen + 1} (Ctrl+C to stop)", flush=True)
    try:
        while True:
            _, out, _ = run(client, f"wc -l < {EVENTS_LOG}")
            total = int((out.strip() or "0"))
            if total > seen:
                _, chunk, _ = run(client, f"tail -n +{seen + 1} {EVENTS_LOG}")
                sys.stdout.buffer.write(chunk.encode("utf-8", errors="replace"))
                sys.stdout.flush()
                seen = total
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        client.close()
    return 0


def cmd_upload_fw(args: argparse.Namespace) -> int:
    local = Path(args.local_file)
    if not local.is_file():
        print(f"Файл не найден: {local}", file=sys.stderr)
        return 1
    remote = f"{FW_CACHE}/{local.name}"
    client = connect(args.host, args.user, args.password)
    sftp = client.open_sftp()
    sftp.put(str(local), remote)
    sftp.chmod(remote, 0o644)
    sftp.close()
    run(client, f"chown sa02m-flasher:sa02m-flasher {remote}")
    client.close()
    print(f"Uploaded {local.name} -> {remote}")
    print("Перезапустите список прошивок в веб-UI или: POST /api/flasher/firmware/refresh")
    return 0


def cmd_hostkey(args: argparse.Namespace) -> int:
    print("Текущий hostkey для plink -hostkey (обновите после перепрошивки образа):")
    print(DEFAULT_HOSTKEY)
    print()
    print("Получить заново с ПК:")
    print(f"  ssh-keyscan -t ed25519 {args.host}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="SSH helper for SA-02m agents")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--password", default=DEFAULT_PASS)
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("exec", help="Выполнить одну команду на устройстве")
    pe.add_argument("command")
    pe.add_argument("--timeout", type=float, default=120.0)
    pe.set_defaults(func=cmd_exec)

    pt = sub.add_parser("tail-flasher", help="Хвост events.log прошивальщика")
    pt.add_argument("-n", "--lines", type=int, default=30)
    pt.set_defaults(func=cmd_tail_flasher)

    pw = sub.add_parser("watch-flasher", help="Следить за events.log во время прошивки")
    pw.set_defaults(func=cmd_watch_flasher)

    pu = sub.add_parser("upload-fw", help="Загрузить .fw в кеш sa02m-flasher на устройстве")
    pu.add_argument("local_file")
    pu.set_defaults(func=cmd_upload_fw)

    ph = sub.add_parser("hostkey", help="Показать hostkey для plink")
    ph.set_defaults(func=cmd_hostkey)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
