#!/usr/bin/env python3
import serial, time, sys, re

s = serial.Serial("COM7", 115200, timeout=0.2)
time.sleep(0.5); s.read(s.in_waiting)
s.write(b"\n"); time.sleep(1.0)
raw = s.read(s.in_waiting).decode(errors="replace")
if "login:" in raw:
    s.write(b"root\n"); time.sleep(1.0); s.read(s.in_waiting)
    s.write(b"cyntron\n"); time.sleep(1.5); s.read(s.in_waiting)
s.write(b"\n"); time.sleep(0.8); s.read(s.in_waiting)

def run(cmd, wait=8):
    s.read(s.in_waiting)
    s.write((cmd + "\n").encode())
    buf = b""; t = time.time() + wait
    while time.time() < t:
        c = s.read(s.in_waiting or 1)
        if c: buf += c; t = time.time() + 1.2
        else: time.sleep(0.05)
    return re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\r', '', buf.decode(errors="replace"))

cmds = [
    "ip route",
    "ip addr show enx344b50000000",
    "ping -c3 -W3 192.168.0.1 2>&1 | tail -4",
    "curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=network_type,rssi,wan_ipaddr,ppp_status,modem_main_state' 2>/dev/null",
    "ping -c3 -W3 8.8.8.8 2>&1 | tail -4",
]
for c in cmds:
    print(f"\n$ {c}")
    print(run(c))
s.close()
