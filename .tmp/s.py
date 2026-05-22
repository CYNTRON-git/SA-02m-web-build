#!/usr/bin/env python3
"""Minimal serial: wait for prompt, run one command, print output."""
import serial, time, sys

s = serial.Serial("COM7", 115200, timeout=0.2)
time.sleep(0.5); s.read(s.in_waiting)

# Wake / get prompt
for _ in range(3):
    s.write(b"\n"); time.sleep(1.0)
    raw = s.read(s.in_waiting)
    if b"# " in raw: break

def run(cmd, wait=10):
    s.read(s.in_waiting)
    s.write((cmd + "\n").encode())
    buf = b""; t = time.time() + wait
    while time.time() < t:
        c = s.read(s.in_waiting or 1)
        if c: buf += c; t = time.time() + 1.5
        else: time.sleep(0.05)
    return buf.decode(errors="replace")

cmds = sys.argv[1:] if len(sys.argv) > 1 else [
    "dpkg -l modemmanager ppp 2>/dev/null | awk '/^ii/{print $2,$3}'",
    "which mmcli pppd 2>/dev/null; echo ---",
    "systemctl is-active ModemManager.service",
]
for c in cmds:
    out = run(c)
    # strip escape codes
    import re; out = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', out)
    print(f"$ {c}\n{out}")

s.close()
