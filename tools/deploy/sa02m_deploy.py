#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Деплой файлов на SA-02m через SFTP (dev/hotfix).

Примеры:
  py -3 tools/deploy/sa02m_deploy.py \\
    www/network_config/static/js/flasher.js:/var/www/network_config/static/js/flasher.js \\
    --post "systemctl restart sa02m-flasher"

  py -3 tools/deploy/sa02m_deploy.py --profile flasher-module-config

Переменные: SA02M_HOST, SA02M_USER, SA02M_PASS
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError as exc:
    print("pip install paramiko", file=sys.stderr)
    raise SystemExit(2) from exc

REPO_ROOT = Path(__file__).resolve().parents[2]

PROFILES: dict[str, list[tuple[str, str]]] = {
    "flasher-web": [
        ("www/network_config/static/js/flasher.js", "/var/www/network_config/static/js/flasher.js"),
        ("www/network_config/index.html", "/var/www/network_config/index.html"),
    ],
    "flasher-module-config": [
        ("opt/sa02m-flasher/sa02m_flasher/mplc_lease.py", "/opt/sa02m-flasher/sa02m_flasher/mplc_lease.py"),
        ("opt/sa02m-flasher/sa02m_flasher/service.py", "/opt/sa02m-flasher/sa02m_flasher/service.py"),
        ("www/network_config/static/js/flasher.js", "/var/www/network_config/static/js/flasher.js"),
        ("www/network_config/index.html", "/var/www/network_config/index.html"),
    ],
    "flasher-daemon": [
        ("opt/sa02m-flasher/sa02m_flasher/jobs.py", "/opt/sa02m-flasher/sa02m_flasher/jobs.py"),
        ("opt/sa02m-flasher/sa02m_flasher/service.py", "/opt/sa02m-flasher/sa02m_flasher/service.py"),
        ("opt/sa02m-flasher/sa02m_flasher/runner.py", "/opt/sa02m-flasher/sa02m_flasher/runner.py"),
    ],
    "services-ui": [
        ("www/network_config/static/js/app.js", "/var/www/network_config/static/js/app.js"),
        ("etc/sa02m-web-service-ctl.sh", "/usr/local/sbin/sa02m-web-service-ctl.sh"),
        ("www/network_config/cgi-bin/services_ctrl.cgi", "/var/www/network_config/cgi-bin/services_ctrl.cgi"),
    ],
}


def parse_pair(raw: str) -> tuple[Path, str]:
    if ":" not in raw:
        raise ValueError(f"ожидается local:remote, получено: {raw!r}")
    local_s, remote = raw.split(":", 1)
    local = REPO_ROOT / local_s.replace("\\", "/")
    if not local.is_file():
        raise FileNotFoundError(local)
    return local, remote


def deploy(pairs: list[tuple[Path, str]], *, host: str, user: str, password: str, post: str | None) -> int:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=20, allow_agent=False, look_for_keys=False)
    sftp = client.open_sftp()
    for local, remote in pairs:
        sftp.open(remote, "wb").write(local.read_bytes())
        print("uploaded", remote)
    sftp.close()
    if post:
        _, stdout, stderr = client.exec_command(post, timeout=120)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if out:
            print(out, end="" if out.endswith("\n") else "\n")
        if err:
            print(err, file=sys.stderr, end="" if err.endswith("\n") else "\n")
    client.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Deploy files to SA-02m via SFTP")
    ap.add_argument("pairs", nargs="*", help="local/path:remote/path")
    ap.add_argument("--profile", choices=sorted(PROFILES.keys()), help="готовый набор файлов")
    ap.add_argument("--post", help="shell-команда на устройстве после загрузки")
    ap.add_argument("--host", default=os.environ.get("SA02M_HOST", "192.168.1.136"))
    ap.add_argument("--user", default=os.environ.get("SA02M_USER", "root"))
    ap.add_argument("--password", default=os.environ.get("SA02M_PASS", "cyntron"))
    args = ap.parse_args()

    if args.profile:
        pairs = [(REPO_ROOT / loc, rem) for loc, rem in PROFILES[args.profile]]
        for loc, _ in PROFILES[args.profile]:
            if not (REPO_ROOT / loc).is_file():
                print(f"нет файла: {REPO_ROOT / loc}", file=sys.stderr)
                return 1
    elif args.pairs:
        pairs = [parse_pair(p) for p in args.pairs]
    else:
        ap.error("укажите pairs или --profile")

    return deploy(pairs, host=args.host, user=args.user, password=args.password, post=args.post)


if __name__ == "__main__":
    raise SystemExit(main())
