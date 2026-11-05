#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture SA-02m eMMC image over Windows SSH (paramiko), then finalize in WSL.

Use when WSL cannot reach the donor LAN IP. Pipeline matches make-image.sh:
cleanup → stream-after-cleanup (zerofill + id reset + dd, sha256 of the stream)
→ verify sha256 of the WSL copy → PiShrink → patch-firstboot-image.sh → xz.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
PATCH_SCRIPT_REL = "tools/imaging/patch-firstboot-image.sh"


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
    digest = hashlib.sha256()
    last_report = time.time()
    out_img.parent.mkdir(parents=True, exist_ok=True)
    sidecar = sha256_sidecar(out_img)
    # A sidecar must only ever describe a size-checked stream of THIS run.
    sidecar.unlink(missing_ok=True)
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
                digest.update(chunk)
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
            digest.update(chunk)
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
    line = f"{digest.hexdigest()}  {out_img.name}\n"
    sidecar.write_text(line, encoding="utf-8", newline="\n")
    print(f"[stream] sha256: {line.strip()}", flush=True)


def sha256_sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def read_raw_sha256(raw_img: Path) -> str:
    """Expected sha256 of the raw stream, from its sha256sum-format sidecar."""
    sidecar = sha256_sidecar(raw_img)
    try:
        text = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"raw sha256 sidecar unreadable: {sidecar} ({exc})")
    m = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)\n?", text)
    if not m or m.group(2) != raw_img.name:
        raise SystemExit(f"raw sha256 sidecar malformed: {sidecar}")
    return m.group(1)


# Values arrive as positional parameters ($1..$5), never interpolated into the
# text: a ' in --name or in a path used to break out of the old '...' literals.
FINALIZE_SCRIPT = r"""
set -euo pipefail
IMG=$1 OUT=$2 NAME=$3 REPO=$4 RAW_SHA=$5
PATCH="$REPO/tools/imaging/patch-firstboot-image.sh"
die() { echo "[finalize] FATAL: $*" >&2; exit 1; }

echo "[finalize] preflight"
for t in pishrink.sh xz sha256sum python3 losetup; do
  command -v "$t" >/dev/null 2>&1 || die "$t not found in WSL"
done
[ -r "$PATCH" ] || die "patch script not readable: $PATCH"

# Native fs for a multi-GiB work copy (not drvfs, not a tmpfs /tmp).
WORK=$(mktemp -d /var/tmp/sa02m-finalize-XXXXXX)
trap 'rm -rf "$WORK"' EXIT

echo "[finalize] copy raw + verify sha256"
cp -f "$IMG" "$WORK/$NAME.img"
printf '%s  %s\n' "$RAW_SHA" "$NAME.img" > "$WORK/raw.sha256"
( cd "$WORK" && sha256sum -c raw.sha256 ) || die "sha256 of the WSL copy != the stream's"

echo "[finalize] PiShrink"
sudo pishrink.sh -a -v "$WORK/$NAME.img"

# Unconditional and fatal, as in make-image.sh: canonical boot.scr, first-boot
# wiring and the image-identity wipe + check (docs/contracts/image-identity-reset.md
# site 3). Through bash: the file is tracked mode 644.
echo "[finalize] patch-firstboot-image.sh"
sudo bash "$REPO/tools/imaging/patch-firstboot-image.sh" "$WORK/$NAME.img"

echo "[finalize] publish + xz"
cp -f "$WORK/$NAME.img" "$OUT/$NAME.img"
xz -T0 -9e -v -c "$WORK/$NAME.img" > "$OUT/$NAME.img.xz"
( cd "$OUT" && sha256sum "$NAME.img" > "$NAME.img.sha256" && sha256sum "$NAME.img.xz" > "$NAME.img.xz.sha256" )
ls -lh "$OUT/$NAME.img" "$OUT/$NAME.img.xz"
"""


def finalize_wsl(
    raw_img: Path, name: str, out_dir: Path, source_ip: str, *, cleanup: bool, zerofill: bool, id_reset: bool
) -> None:
    raw_sha = read_raw_sha256(raw_img)
    wsl_img = subprocess.check_output(["wsl", "-e", "wslpath", "-a", str(raw_img)], text=True).strip()
    wsl_out = subprocess.check_output(["wsl", "-e", "wslpath", "-a", str(out_dir)], text=True).strip()
    wsl_repo = subprocess.check_output(["wsl", "-e", "wslpath", "-a", str(REPO)], text=True).strip()
    print("[finalize] sha256 verify + PiShrink + patch-firstboot-image.sh + xz in WSL", flush=True)
    subprocess.check_call(
        ["wsl", "-e", "bash", "-lc", FINALIZE_SCRIPT, "_", wsl_img, wsl_out, name, wsl_repo, raw_sha]
    )

    xz_path = out_dir / f"{name}.img.xz"
    img_path = out_dir / f"{name}.img"
    doc = {
        "image_name": xz_path.name,
        "image_sha256": (out_dir / f"{name}.img.xz.sha256").read_text(encoding="utf-8").split()[0],
        "img_name": img_path.name,
        "img_sha256": (out_dir / f"{name}.img.sha256").read_text(encoding="utf-8").split()[0],
        "raw_sha256": raw_sha,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "pipeline": {
            "tool": "capture-image-win.py",
            "pishrink": True,
            "patch_firstboot": True,
            "patch_script": PATCH_SCRIPT_REL,
            "cleanup": cleanup,
            "zerofill": zerofill,
            "id_reset_in_stream": id_reset,
        },
        "source_device": {
            "ip": source_ip,
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
            print("[1/4] cleanup-donor.sh --apply --report", flush=True)
            rc = run_script_file(client, cleanup, args="--apply --report", timeout=3600)
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
    finalize_wsl(
        raw_img,
        args.name,
        out_dir,
        args.ip,
        cleanup=not args.no_cleanup,
        zerofill=not args.no_zerofill,
        id_reset=not args.no_id_reset,
    )

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
