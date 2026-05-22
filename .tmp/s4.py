#!/usr/bin/env python3
import serial, time, re

s = serial.Serial("COM7", 115200, timeout=0.2)
time.sleep(0.3); s.read(s.in_waiting)
s.write(b"\n"); time.sleep(0.5); s.read(s.in_waiting)

def run(cmd, wait=8):
    s.read(s.in_waiting)
    s.write((cmd + "\n").encode())
    buf = b""; t = time.time() + wait
    while time.time() < t:
        c = s.read(s.in_waiting or 1)
        if c: buf += c; t = time.time() + 1.5
        else: time.sleep(0.05)
    return re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\r', '', buf.decode(errors="replace"))

# Восстановить eth0 с onlink
print("=== Restore eth0 onlink ===")
print(run("ip route del default dev eth0 2>/dev/null; ip route del default via 192.168.1.1 dev eth0 2>/dev/null; ip route add default via 192.168.1.1 dev eth0 onlink; ip route"))

print("=== eth0 link/operstate ===")
print(run("cat /sys/class/net/eth0/carrier /sys/class/net/eth0/operstate 2>/dev/null"))

print("=== SSH test ===")
print(run("echo 'SSH OK'"))

s.close()
print("\nDone. Checking SSH...")
