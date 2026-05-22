#!/usr/bin/env python3
"""Restore SA-02m donor after aborted imaging via serial (COM7 115200).

Usage: py serial-restore-ssh.py [COM7] [--preflight]
"""
import sys
import time

try:
    import serial
except ImportError:
    print("py -3 -m pip install pyserial", file=sys.stderr)
    sys.exit(2)

PORT = "COM7"
PREFLIGHT = False
for arg in sys.argv[1:]:
    if arg == "--preflight":
        PREFLIGHT = True
    elif not arg.startswith("-"):
        PORT = arg

BAUD = 115200
ROOT_PASS = "cyntron"

RESTORE_CMDS = [
    "pkill -TERM -f 'dd if=/dev/mmcblk2' 2>/dev/null || true",
    "sleep 1",
    "pkill -KILL -f 'dd if=/dev/mmcblk2' 2>/dev/null || true",
    "rm -f /zero.fill",
]
if not PREFLIGHT:
    RESTORE_CMDS += [
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
        "wc -c /etc/machine-id",
        "ip -4 -o addr show eth0 | awk '{print $4}'",
    ]


def drain(ser, t=0.5):
    time.sleep(t)
    buf = b""
    while ser.in_waiting:
        buf += ser.read(ser.in_waiting)
        time.sleep(0.03)
    return buf


def send_line(ser, text, wait=1.0):
    ser.write((text + "\r\n").encode())
    out = drain(ser, wait).decode("utf-8", errors="replace")
    print(f">>> {text}")
    print(out, end="")
    return out


def ensure_root_shell(ser):
    out = send_line(ser, "", 0.3)
    if "login:" in out.lower():
        out = send_line(ser, "root", 1.0)
        out = send_line(ser, ROOT_PASS, 2.0)
    if "Choose default system command shell" in out:
        out = send_line(ser, "1", 2.0)
    if "Please provide a username" in out:
        ser.write(b"\x03")
        drain(ser, 2.0)
    return out


def main():
    ser = serial.Serial(PORT, BAUD, timeout=0.2)
    ser.dtr = True
    ser.rts = False
    time.sleep(0.2)
    ensure_root_shell(ser)

    all_out = ""
    for cmd in RESTORE_CMDS:
        wait = 15.0 if "ssh-keygen" in cmd else (2.0 if "sleep" in cmd else 1.5)
        all_out += send_line(ser, cmd, wait)

    ser.close()
    if PREFLIGHT:
        return 0
    return 0 if "SSH_STATE=0" in all_out or "\nactive" in all_out else 1


if __name__ == "__main__":
    sys.exit(main())
