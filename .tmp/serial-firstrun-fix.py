#!/usr/bin/env python3
"""Abort Armbian firstrun wizard on COM7 and restore root SSH for imaging."""
import sys
import time

import serial

PORT = "COM7"
BAUD = 115200
PASS = "cyntron"

CMDS = [
    "pkill -TERM -f 'dd if=/dev/mmcblk2' 2>/dev/null || true",
    "pkill -KILL -f 'dd if=/dev/mmcblk2' 2>/dev/null || true",
    "rm -f /zero.fill /run/sa02m-imaging.lock",
    "test -s /etc/machine-id || systemd-machine-id-setup",
    "test -L /var/lib/dbus/machine-id || ln -sf /etc/machine-id /var/lib/dbus/machine-id",
    "test -f /etc/ssh/ssh_host_ed25519_key || ssh-keygen -A",
    "systemctl enable regen-ssh-host-keys.service 2>/dev/null || true",
    "systemctl enable ssh",
    "systemctl restart ssh",
    "rm -f /root/.not_logged_in_yet",
    "systemctl start nginx fcgiwrap sa02m-flasher php8.3-fpm 2>/dev/null || true",
    "sync",
    "systemctl is-active ssh; echo SSH_STATE=$?",
    "ip -4 -o addr show eth0 | awk '{print $4}'",
]


def drain(s, t=0.4):
    time.sleep(t)
    out = b""
    while s.in_waiting:
        out += s.read(s.in_waiting)
        time.sleep(0.03)
    return out.decode("utf-8", errors="replace")


def wake(s):
    for _ in range(3):
        s.write(b"\x03")
        time.sleep(0.4)
    s.write(b"\r\n")
    drain(s, 0.8)


def try_login(s):
    out = drain(s, 0.2)
    if "login:" in out.lower():
        s.write(b"root\r\n")
        drain(s, 1.0)
        s.write((PASS + "\r\n").encode())
        return drain(s, 2.0)
    if "password" in out.lower():
        s.write((PASS + "\r\n").encode())
        return drain(s, 2.0)
    return out


def main():
    s = serial.Serial(PORT, BAUD, timeout=0.2)
    s.dtr = True
    s.rts = False
    time.sleep(0.3)
    print("wake + abort firstrun", file=sys.stderr)
    wake(s)
    out = try_login(s)
    if "#" not in out and "root@" not in out:
        wake(s)
        out = try_login(s)
    if "#" not in out:
        print("WARN: shell prompt not seen, continuing anyway", file=sys.stderr)
        print(out[-500:], file=sys.stderr)

    for cmd in CMDS:
        s.write((cmd + "\r\n").encode())
        chunk = drain(s, 2.5 if "ssh-keygen" in cmd else 1.2)
        print(f">>> {cmd}")
        print(chunk, end="")
    s.close()
    return 0 if "SSH_STATE=active" in chunk or "SSH_STATE=0" in chunk else 1


if __name__ == "__main__":
    sys.exit(main())
