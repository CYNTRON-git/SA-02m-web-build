#!/usr/bin/env python3
import serial, time, sys

PORT = "COM7"
BAUD = 115200

def cmd(s, text, wait=5.0):
    s.read(s.in_waiting)  # flush
    s.write((text + "\n").encode())
    time.sleep(wait)
    out = b""
    t = time.time() + 2
    while time.time() < t:
        c = s.read(s.in_waiting or 1)
        if c: out += c; t = time.time() + 1
        else: time.sleep(0.05)
    return out.decode(errors="replace")

s = serial.Serial(PORT, BAUD, timeout=0.3)
time.sleep(0.5); s.read(s.in_waiting)
s.write(b"\n"); time.sleep(0.8); s.read(s.in_waiting)

checks = [
    ("pkg check",   "dpkg -l modemmanager ppp isc-dhcp-client 2>/dev/null | awk '/^ii/{printf \"%s %s\\n\",$2,$3}'"),
    ("mmcli/pppd",  "ls -la /usr/sbin/pppd /usr/bin/mmcli 2>/dev/null; echo ---"),
    ("MM status",   "systemctl is-active ModemManager.service 2>/dev/null || echo inactive"),
    ("update?",     "apt-cache policy modemmanager 2>/dev/null | grep -E 'Installed|Candidate' | head -4"),
]

for name, c in checks:
    print(f"\n=== {name} ===")
    print(cmd(s, c, wait=3.0))

s.close()
