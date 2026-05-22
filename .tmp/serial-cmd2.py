#!/usr/bin/env python3
"""Отправить команду через COM7 и получить вывод."""
import serial, sys, time, re

PORT = "COM7"
BAUD = 115200

cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "ip addr show eth0; echo ---DONE---"

try:
    s = serial.Serial(PORT, BAUD, timeout=0.5)
except Exception as e:
    print(f"Cannot open {PORT}: {e}"); sys.exit(1)

time.sleep(0.3)
s.read(s.in_waiting)  # flush

s.write(b"\n")
time.sleep(0.3)
s.read(s.in_waiting)

s.write((cmd + "\n").encode())
time.sleep(0.3)

buf = b""
deadline = time.time() + 8
while time.time() < deadline:
    chunk = s.read(512)
    if chunk:
        buf += chunk
        deadline = time.time() + 2  # reset on data
    else:
        time.sleep(0.1)

s.close()
print(buf.decode(errors="replace"))
