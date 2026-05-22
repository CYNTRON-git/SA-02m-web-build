#!/usr/bin/env python3
"""Login root/cyntron via serial, run commands, print output."""
import serial, sys, time

PORT = "COM7"
BAUD = 115200

def readall(s, timeout=3.0, idle=0.5):
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = s.read(min(512, s.in_waiting or 1))
        if chunk:
            buf += chunk
            deadline = time.time() + idle
        else:
            time.sleep(0.05)
    return buf.decode(errors="replace")

s = serial.Serial(PORT, BAUD, timeout=0.2)
time.sleep(0.3); s.read(s.in_waiting)

# Wake
s.write(b"\n"); raw = readall(s, 2.0, 0.4)
print("BOOT:", repr(raw[-100:]))

if "login:" in raw:
    s.write(b"root\n"); raw = readall(s, 2.0, 0.4)
    print("LOGIN:", repr(raw[-100:]))
    if "assword" in raw:
        s.write(b"cyntron\n"); raw = readall(s, 2.5, 0.5)
        print("PASS:", repr(raw[-100:]))

# Flush prompt
s.write(b"\n"); readall(s, 1.5, 0.3)

cmds = [
    "ip addr show eth0",
    "ip route",
    "ping -c 2 -W 2 8.8.8.8 2>&1 || echo PING-FAIL",
    "dhclient -v eth0 2>&1 | tail -8",
    "ip addr show eth0",
    "ip route",
    "ping -c 2 -W 2 8.8.8.8 2>&1 && echo INET-OK || echo INET-FAIL",
]

for cmd in cmds:
    print(f"\n>>> {cmd}")
    s.write((cmd + "\n").encode())
    out = readall(s, 8.0, 1.0)
    print(out)

s.close()
