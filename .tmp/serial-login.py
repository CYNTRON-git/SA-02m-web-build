#!/usr/bin/env python3
"""Login via serial and run commands."""
import serial, sys, time

PORT = "COM7"
BAUD = 115200

commands = sys.argv[1:] if len(sys.argv) > 1 else []

def send(s, text, wait=0.8):
    s.write((text + "\n").encode())
    time.sleep(wait)
    return s.read(s.in_waiting).decode(errors="replace")

try:
    s = serial.Serial(PORT, BAUD, timeout=0.5)
except Exception as e:
    print(f"Cannot open {PORT}: {e}"); sys.exit(1)

time.sleep(0.3)
raw = s.read(s.in_waiting).decode(errors="replace")
print("INIT:", repr(raw[-80:]))

# Wake up
s.write(b"\n"); time.sleep(0.5)
raw = s.read(s.in_waiting).decode(errors="replace")
print("AFTER_NEWLINE:", repr(raw[-120:]))

# If at login prompt
if "login:" in raw.lower() or "login:" in repr(raw).lower():
    print(">>> Sending login: root")
    out = send(s, "root", 1.0)
    print("after user:", repr(out[-100:]))
    if "password" in out.lower() or "Password" in out:
        print(">>> Sending password")
        out2 = send(s, "cyntron", 1.5)
        print("after pass:", repr(out2[-150:]))
    else:
        time.sleep(1.5)
        out2 = s.read(s.in_waiting).decode(errors="replace")
        print("no pass prompt:", repr(out2[-150:]))
else:
    print("Not at login prompt, assuming already logged in")

# Clear prompt
s.write(b"\n"); time.sleep(0.5)
s.read(s.in_waiting)

# Run commands
for cmd in commands:
    print(f"\n=== CMD: {cmd} ===")
    s.write((cmd + "\n").encode())
    time.sleep(0.3)
    buf = b""
    deadline = time.time() + 6
    while time.time() < deadline:
        chunk = s.read(512)
        if chunk:
            buf += chunk
            deadline = time.time() + 1.5
        else:
            time.sleep(0.1)
    print(buf.decode(errors="replace"))

s.close()
