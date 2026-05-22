#!/usr/bin/env python3
import serial, time, sys

PORT  = "COM7"
BAUD  = 115200
CRLF  = b"\r\n"

def waitprompt(s, prompt=b"#", timeout=20):
    buf = b""
    t = time.time() + timeout
    while time.time() < t:
        c = s.read(s.in_waiting or 1)
        if c:
            buf += c
            sys.stdout.write(c.decode(errors="replace"))
            sys.stdout.flush()
            if prompt in buf[-20:]:
                return buf
        else:
            time.sleep(0.05)
    return buf

def run(s, cmd, timeout=30):
    s.read(s.in_waiting)  # discard
    s.write((cmd + "\n").encode())
    return waitprompt(s, b"# ", timeout)

s = serial.Serial(PORT, BAUD, timeout=0.2)
time.sleep(0.5); s.read(s.in_waiting)

print("=== Ctrl+C to kill running process ===")
s.write(b"\x03"); time.sleep(0.5)
s.write(b"\x03"); time.sleep(0.5)
s.write(b"\n");   time.sleep(0.5)
raw = s.read(s.in_waiting).decode(errors="replace")
print(repr(raw[-80:]))

# wait for prompt
waitprompt(s, b"# ", 3)

print("\n=== kill any dpkg/apt locks ===")
run(s, "rm -f /var/lib/dpkg/lock* /var/cache/apt/archives/lock 2>/dev/null; echo ok", 5)

print("\n=== apt-get update ===")
run(s, "apt-get update -q 2>&1 | tail -3", 90)

print("\n=== install modemmanager ppp ===")
run(s, "DEBIAN_FRONTEND=noninteractive apt-get -y install modemmanager ppp 2>&1 | tail -5", 120)

print("\n=== verify ===")
run(s, "dpkg -l modemmanager ppp | awk '/^ii/{print $2,$3}'", 5)
run(s, "systemctl enable --now ModemManager.service && systemctl is-active ModemManager.service", 10)

print("\n=== DONE ===")
s.close()
