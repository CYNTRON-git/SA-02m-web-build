#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture SA-02m eMMC image over Windows SSH (paramiko), then PiShrink in WSL.

Use when WSL cannot reach the donor LAN IP. Pipeline matches make-image.sh:
cleanup → stream-after-cleanup (zerofill + id reset + dd) → pishrink → xz.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import paramiko

EMMC_BYTES = 7818182656
REPO = Path(__file__).resolve().parents[2]
IMAGING = Path(__file__).resolve().parent
ENV_FILE = REPO / "tools" / "sa02m-device.env"


def load_env() -> dict[str, str]:
    data: dict[str, str] = {}
    if not ENV_FILE.is_file():
        return data
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
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


def exec_bash(client: paramiko.SSHClient, script: str, timeout: float = 120.0) -> int:
    stdin, stdout, stderr = client.exec_command("bash -s", timeout=timeout, get_pty=False)
    stdin.write(script.encode("utf-8"))
    stdin.channel.shutdown_write()
    out = stdout.read()
    err = stderr.read()
    if out:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
    if err:
        sys.stderr.buffer.write(err)
        sys.stderr.buffer.flush()
    return stdout.channel.recv_exit_status()


def run_script_file(client: paramiko.SSHClient, script: Path, args: str = "", timeout: float = 7200.0) -> int:
    cmd = f"bash -s -- {args}".rstrip()
    print(f"[run] {script.name} {args}".rstrip(), flush=True)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout, get_pty=False)
    stdin.write(script.read_bytes())
    stdin.channel.shutdown_write()
    while not stderr.channel.exit_status_ready():
        if stderr.channel.recv_stderr_ready():
            chunk = stderr.channel.recv_stderr(65536)
            if chunk:
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
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
    return stdout.channel.recv_exit_status()


def stream_image(client: paramiko.SSHClient, script: Path, out_img: Path, args: str = "") -> None:
    cmd = f"bash -s -- {args}".rstrip()
    print(f"[stream] {script.name} -> {out_img}", flush=True)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=None, get_pty=False)
    stdin.write(script.read_bytes())
    stdin.channel.shutdown_write()

    written = 0
    last_report = time.time()
    out_img.parent.mkdir(parents=True, exist_ok=True)
    with out_img.open("wb") as out:
        while True:
            if stderr.channel.recv_stderr_ready():
                err = stderr.channel.recv_stderr(65536)
                if err:
                    sys.stderr.buffer.write(err)
                    sys.stderr.buffer.flush()
            chunk = stdout.channel.recv(1024 * 1024)
            if chunk:
                out.write(chunk)
                written += len(chunk)
                now = time.time()
                if now - last_report >= 10:
                    pct = 100.0 * written / EMMC_BYTES
                    print(
                        f"[stream] {written / (1024**3):.2f} GiB / "
                        f"{EMMC_BYTES / (1024**3):.2f} GiB ({pct:.1f}%)",
                        flush=True,
                    )
                    last_report = now
                continue
            if stdout.channel.exit_status_ready() and not stdout.channel.recv_ready():
                break
            time.sleep(0.05)
        while stdout.channel.recv_ready():
            chunk = stdout.channel.recv(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
        while stderr.channel.recv_stderr_ready():
            err = stderr.channel.recv_stderr(65536)
            if err:
                sys.stderr.buffer.write(err)
                sys.stderr.buffer.flush()
        out.flush()
        os.fsync(out.fileno())

    code = stdout.channel.recv_exit_status()
    print(f"[stream] done: {written} bytes, ssh_rc={code}", flush=True)
    if written != EMMC_BYTES:
        raise SystemExit(f"raw size {written} != expected {EMMC_BYTES}")


def finalize_wsl(raw_img: Path, name: str, out_dir: Path) -> None:
    wsl_img = subprocess.check_output(["wsl", "-e", "wslpath", "-a", str(raw_img)], text=True).strip()
    wsl_out = subprocess.check_output(["wsl", "-e", "wslpath", "-a", str(out_dir)], text=True).strip()
    wsl_repo = subprocess.check_output(["wsl", "-e", "wslpath", "-a", str(REPO)], text=True).strip()
    script = f"""
set -euo pipefail
IMG='{wsl_img}'
OUT='{wsl_out}'
NAME='{name}'
REPO='{wsl_repo}'
WORK=$(mktemp -d /tmp/sa02m-finalize-XXXXXX)
trap 'rm -rf "$WORK"' EXIT
cp -f "$IMG" "$WORK/$NAME.img"
sudo pishrink.sh -a -v "$WORK/$NAME.img"
loop=$(sudo losetup --partscan -f --show "$WORK/$NAME.img" 2>/dev/null || true)
if [ -n "$loop" ]; then
  mnt=$(mktemp -d /tmp/sa02m-patch-XXXXXX)
  rootpart="${{loop}}p2"
  for i in 1 2 3 4 5 6 8 10; do [ -b "$rootpart" ] && break; sleep 1; done
  if sudo mount "$rootpart" "$mnt" 2>/dev/null; then
    sudo cp -f "$REPO/etc/systemd/sa02m-userspace-watchdog.service" "$mnt/etc/systemd/system/" 2>/dev/null || true
    sudo cp -f "$REPO/etc/net-watchdog.service" "$mnt/etc/systemd/system/" 2>/dev/null || true
    sudo cp -f "$REPO/etc/sa02m-failure-monitor.service" "$mnt/etc/systemd/system/" 2>/dev/null || true
    sudo install -d "$mnt/etc/systemd/system.conf.d"
    sudo cp -f "$REPO/etc/systemd/sa02m-watchdog.conf" "$mnt/etc/systemd/system.conf.d/" 2>/dev/null || true
    sudo sed -i 's/^RuntimeWatchdogSec=/#RuntimeWatchdogSec=/' "$mnt/etc/systemd/system.conf" 2>/dev/null || true
    sudo rm -f "$mnt/var/lib/sa02m-rootfs-expand.done"
    sudo umount "$mnt"
  fi
  sudo losetup -d "$loop" 2>/dev/null || true
  rm -rf "$mnt"
fi
cp -f "$WORK/$NAME.img" "$OUT/$NAME.img"
xz -T0 -9e -v -c "$WORK/$NAME.img" > "$OUT/$NAME.img.xz"
( cd "$OUT" && sha256sum "$NAME.img" > "$NAME.img.sha256" && sha256sum "$NAME.img.xz" > "$NAME.img.xz.sha256" )
ls -lh "$OUT/$NAME.img" "$OUT/$NAME.img.xz"
"""
    print("[finalize] PiShrink + xz in WSL", flush=True)
    subprocess.check_call(["wsl", "-e", "bash", "-lc", script])

    xz_path = out_dir / f"{name}.img.xz"
    img_path = out_dir / f"{name}.img"
    doc = {
        "image_name": xz_path.name,
        "image_sha256": (out_dir / f"{name}.img.xz.sha256").read_text(encoding="utf-8").split()[0],
        "img_sha256": (out_dir / f"{name}.img.sha256").read_text(encoding="utf-8").split()[0],
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "pipeline": {
            "tool": "capture-image-win.py",
            "pishrink": True,
            "cleanup": True,
            "id_reset_in_stream": True,
        },
        "source_device": {
            "ip": "192.168.1.136",
            "emmc_device": "/dev/mmcblk2",
            "emmc_size_gib": 7.28,
        },
    }
    (out_dir / f"{name}.manifest.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] {img_path}", flush=True)
    print(f"[ok] {xz_path}", flush=True)


def main() -> int:
    env = load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=os.environ.get("SA02M_HOST", env.get("SA02M_HOST", "192.168.1.136")))
    ap.add_argument("--user", default=os.environ.get("SA02M_USER", env.get("SA02M_USER", "root")))
    ap.add_argument("--password", default=os.environ.get("SA02M_PASS", env.get("SA02M_PASS", "cyntron")))
    ap.add_argument("--name", default=time.strftime("SA-02m-%Y%m%d-%H%M"))
    ap.add_argument("--out-dir", default=str(IMAGING / "out"))
    ap.add_argument("--no-cleanup", action="store_true")
    ap.add_argument("--no-zerofill", action="store_true")
    ap.add_argument("--no-id-reset", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    raw_img = out_dir / f"{args.name}.raw.img"
    cleanup = IMAGING / "cleanup-donor.sh"
    stream = IMAGING / "stream-after-cleanup.sh"

    print(f"Donor : {args.user}@{args.ip}", flush=True)
    print(f"Output: {out_dir / (args.name + '.img')}", flush=True)

    client = connect(args.ip, args.user, args.password)
    try:
        print("[0] stop/mask watchdogs", flush=True)
        rc = exec_bash(
            client,
            """set -euo pipefail
for svc in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog sa02m-watchdog-feed; do
  systemctl stop "$svc" 2>/dev/null || true
  systemctl mask "$svc" 2>/dev/null || true
done
systemctl set-property --runtime Manager RuntimeWatchdogSec=0 2>/dev/null || true
sync
echo WATCHDOGS_OFF
""",
        )
        if rc != 0:
            raise SystemExit("watchdog prep failed")

        if not args.no_cleanup:
            print("[1/4] cleanup-donor.sh", flush=True)
            rc = run_script_file(client, cleanup, timeout=3600)
            if rc != 0:
                raise SystemExit(f"cleanup failed rc={rc}")
            client.close()
            client = connect(args.ip, args.user, args.password)
        else:
            print("[1/4] cleanup skipped", flush=True)

        stream_args = []
        if args.no_zerofill:
            stream_args.append("--no-zerofill")
        if args.no_id_reset:
            stream_args.append("--no-id-reset")
        print("[2/4] stream eMMC (do not interrupt)", flush=True)
        stream_image(client, stream, raw_img, " ".join(stream_args))
    finally:
        try:
            client.close()
        except Exception:
            pass

    print("[3/4] finalize in WSL", flush=True)
    finalize_wsl(raw_img, args.name, out_dir)

    print("[4/4] reboot donor (best effort)", flush=True)
    try:
        c2 = connect(args.ip, args.user, args.password)
        c2.exec_command("sync; nohup bash -c 'sleep 2; reboot' >/dev/null 2>&1 &")
        time.sleep(1)
        c2.close()
    except Exception as exc:
        print(f"reboot skipped: {exc}", flush=True)

    # Keep .raw.img until operator confirms; free space later with --keep-raw false
    print("READY", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
