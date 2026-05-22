#!/usr/bin/env python3
"""Run apt install modemmanager ppp via serial COM7."""
import serial, time, sys

PORT = "COM7"
BAUD = 115200

def readall(s, timeout=5.0, idle=1.0):
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = s.read(min(1024, max(1, s.in_waiting)))
        if chunk:
            buf += chunk
            deadline = time.time() + idle
            sys.stdout.write(chunk.decode(errors="replace"))
            sys.stdout.flush()
        else:
            time.sleep(0.05)
    return buf.decode(errors="replace")

def run(s, cmd, timeout=120, idle=2.0):
    print(f"\n\033[33m>>> {cmd}\033[0m")
    s.write((cmd + "\n").encode())
    return readall(s, timeout, idle)

s = serial.Serial(PORT, BAUD, timeout=0.2)
time.sleep(0.3); s.read(s.in_waiting)

# Ensure logged in
s.write(b"\n"); time.sleep(0.5)
raw = s.read(s.in_waiting).decode(errors="replace")
if "login:" in raw:
    s.write(b"root\n"); time.sleep(1.0)
    s.read(s.in_waiting)
    s.write(b"cyntron\n"); time.sleep(1.5)
    s.read(s.in_waiting)

s.write(b"\n"); readall(s, 1.5, 0.3)

run(s, "apt-get update 2>&1 | tail -5", timeout=120, idle=3.0)
run(s, "DEBIAN_FRONTEND=noninteractive apt-get -y install modemmanager ppp 2>&1 | tail -10", timeout=180, idle=5.0)
run(s, "dpkg -l modemmanager ppp 2>/dev/null | awk '/^ii/{print $2,$3}'", timeout=10)
run(s, "which mmcli pppd 2>/dev/null && echo PKG-OK || echo PKG-FAIL", timeout=10)

# Enable ModemManager
run(s, "systemctl enable ModemManager.service && systemctl start ModemManager.service && systemctl is-active ModemManager.service", timeout=15)

print("\n\033[32m=== Done ===\033[0m")
s.close()
