#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""After netinstall reboot: wait for SSH/web, light QA, DONE/FAIL.

Usage:
  python3 postflash-monitor.py [--env stand.env]
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STAND_DIR = Path(__file__).resolve().parent
IMAGING_DIR = STAND_DIR.parent
REPO_ROOT = IMAGING_DIR.parents[1]


def load_env_file(path: Path) -> dict[str, str]:
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


def load_device_env() -> dict[str, str]:
    p = REPO_ROOT / "tools" / "sa02m-device.env"
    return load_env_file(p)


def status_post(base: str, **fields) -> None:
    url = base.rstrip("/") + "/api/status"
    body = urllib.parse.urlencode({k: v for k, v in fields.items() if v is not None}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


def read_status(base: str) -> dict:
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/api/status", timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {}


def parse_leases(path: Path) -> list[tuple[str, str]]:
    """Return list of (ip, mac) from dnsmasq.leases."""
    out: list[tuple[str, str]] = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        # expiry mac ip hostname client-id
        if len(parts) >= 3:
            out.append((parts[2], parts[1]))
    return out


def tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ssh_exec(
    host: str,
    user: str,
    password: str,
    key: str | None,
    command: str,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    try:
        import paramiko
    except ImportError:
        # fallback OpenSSH
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
               "-o", f"ConnectTimeout={int(timeout)}"]
        if key:
            cmd += ["-i", key]
        cmd += [f"{user}@{host}", command]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return p.returncode, p.stdout, p.stderr

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kw: dict = {
        "hostname": host,
        "username": user,
        "timeout": timeout,
        "allow_agent": False,
        "look_for_keys": False,
    }
    if key and Path(key).is_file():
        connect_kw["key_filename"] = key
        connect_kw["look_for_keys"] = True
    else:
        connect_kw["password"] = password
    client.connect(**connect_kw)
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err
    finally:
        client.close()


def apply_watchdog_fix(host: str, user: str, password: str, key: str | None) -> None:
    """Soft firstboot watchdog hygiene (mask→unmask safe RuntimeWatchdogSec)."""
    script = r"""
mkdir -p /etc/systemd/system.conf.d
printf '%s\n' '[Manager]' 'RuntimeWatchdogSec=15s' 'RebootWatchdogSec=0' \
  > /etc/systemd/system.conf.d/sa02m-watchdog.conf
sed -i 's/^RuntimeWatchdogSec=/#RuntimeWatchdogSec=/' /etc/systemd/system.conf 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true
# Do not leave services permanently masked after a good flash
for u in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog; do
  systemctl unmask "$u" 2>/dev/null || true
done
sync
echo OK
"""
    code, out, err = ssh_exec(host, user, password, key, script, timeout=60)
    print(f"[postflash] watchdog fix rc={code} {out.strip()} {err.strip()}", flush=True)


def qa_board(host: str, user: str, password: str, key: str | None, web_port: int) -> tuple[bool, str]:
    code, out, err = ssh_exec(
        host,
        user,
        password,
        key,
        "uname -n; systemctl is-active ssh 2>/dev/null; "
        "systemctl is-active nginx 2>/dev/null || true; "
        "df -h / | tail -1",
        timeout=45,
    )
    if code != 0:
        return False, f"ssh cmd failed: {err or out}"
    web_ok = tcp_open(host, web_port, timeout=2.0)
    msg = out.strip().replace("\n", " | ")
    if not web_ok:
        # nginx may still be starting after expand
        time.sleep(5)
        web_ok = tcp_open(host, web_port, timeout=2.0)
    if not web_ok:
        return False, f"web :{web_port} closed; ssh ok ({msg})"
    return True, msg


def candidates_from_leases(lease_file: Path, stand_ip: str) -> list[str]:
    ips = []
    for ip, _mac in parse_leases(lease_file):
        if ip == stand_ip:
            continue
        ips.append(ip)
    return ips


def scan_subnet(cidr: str, ports: tuple[int, ...] = (22,)) -> list[str]:
    net = ipaddress.ip_network(cidr, strict=False)
    found: list[str] = []
    # Cap scan size for safety
    hosts = list(net.hosts())
    if len(hosts) > 64:
        hosts = hosts[:64]
    for host in hosts:
        ip = str(host)
        if any(tcp_open(ip, p, timeout=0.25) for p in ports):
            found.append(ip)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", type=Path, default=STAND_DIR / "stand.env")
    ap.add_argument("--poll", type=float, default=3.0)
    args = ap.parse_args()

    env = load_env_file(args.env if args.env.is_file() else STAND_DIR / "stand.env.example")
    dev = load_device_env()
    stand_ip = env.get("STAND_IP", "192.168.1.10")
    status_port = int(env.get("STATUS_PORT", "8765"))
    status_base = f"http://127.0.0.1:{status_port}"
    user = env.get("POSTFLASH_SSH_USER", "root")
    password = env.get("POSTFLASH_SSH_PASS") or dev.get("SA02M_PASS", "cyntron")
    key = env.get("POSTFLASH_SSH_KEY") or ""
    if not key:
        cand = REPO_ROOT / "private" / ".ssh" / "sa02m_sa02"
        if cand.is_file():
            key = str(cand)
        else:
            home_key = Path.home() / ".ssh" / "sa02m_sa02"
            if home_key.is_file():
                key = str(home_key)
    timeout = float(env.get("POSTFLASH_TIMEOUT_SEC", "600"))
    web_port = int(env.get("POSTFLASH_WEB_PORT", "9999"))
    lease_file = IMAGING_DIR / "stand-data" / "run" / "dnsmasq.leases"
    expect_net = env.get("POSTFLASH_EXPECT_NET", "")

    print(f"[postflash] watching for boards (timeout={timeout}s)", flush=True)
    known_before: set[str] = set(candidates_from_leases(lease_file, stand_ip))
    session_waiting: str | None = None
    wait_started: float | None = None

    while True:
        st = read_status(status_base)
        state = st.get("state", "IDLE")
        sid = st.get("session_id") or ""

        if state in ("FLASHING", "NETBOOT", "REBOOT_WAIT", "FEL_SEEN"):
            if session_waiting != sid:
                session_waiting = sid
                wait_started = time.time()
                known_before = set(candidates_from_leases(lease_file, stand_ip))
                print(f"[postflash] arm wait session={sid} known={known_before}", flush=True)
            if state == "FLASHING" and st.get("progress", 0) >= 95:
                status_post(
                    status_base,
                    state="REBOOT_WAIT",
                    progress=98,
                    message="waiting SSH after reboot",
                    session_id=sid,
                )

        if session_waiting and wait_started and state in (
            "FLASHING",
            "NETBOOT",
            "REBOOT_WAIT",
        ):
            if time.time() - wait_started > timeout:
                status_post(
                    status_base,
                    state="FAIL",
                    progress=0,
                    message=f"postflash timeout {timeout}s",
                    session_id=session_waiting,
                )
                session_waiting = None
                wait_started = None
                time.sleep(args.poll)
                continue

            # Discover new IPs
            now_leases = set(candidates_from_leases(lease_file, stand_ip))
            new_ips = sorted(now_leases - known_before)
            if not new_ips and expect_net:
                # occasional subnet probe
                if int(time.time()) % 15 < args.poll:
                    new_ips = scan_subnet(expect_net)

            for ip in new_ips:
                if not tcp_open(ip, 22, timeout=1.0):
                    continue
                print(f"[postflash] probing {ip}", flush=True)
                status_post(
                    status_base,
                    state="REBOOT_WAIT",
                    progress=99,
                    message=f"QA {ip}",
                    board_ip=ip,
                    session_id=session_waiting,
                )
                # allow sshd / expand to settle
                time.sleep(8)
                try:
                    apply_watchdog_fix(ip, user, password, key or None)
                    ok, msg = qa_board(ip, user, password, key or None, web_port)
                except Exception as exc:  # noqa: BLE001
                    ok, msg = False, str(exc)
                if ok:
                    status_post(
                        status_base,
                        state="DONE",
                        progress=100,
                        message=msg,
                        board_ip=ip,
                        session_id=session_waiting,
                    )
                    print(f"[postflash] DONE {ip}: {msg}", flush=True)
                else:
                    status_post(
                        status_base,
                        state="FAIL",
                        progress=0,
                        message=msg,
                        board_ip=ip,
                        session_id=session_waiting,
                    )
                    print(f"[postflash] FAIL {ip}: {msg}", flush=True)
                session_waiting = None
                wait_started = None
                break

        # Reset to IDLE a while after DONE/FAIL so next board is obvious
        if state in ("DONE", "FAIL") and st.get("updated"):
            if time.time() - float(st["updated"]) > 90:
                status_post(
                    status_base,
                    state="IDLE",
                    progress=0,
                    message="ready for next board",
                    board_ip="",
                    session_id="",
                )

        time.sleep(args.poll)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[postflash] stop", flush=True)
        raise SystemExit(0)
