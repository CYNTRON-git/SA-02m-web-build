#!/usr/bin/env python3
"""Write a script to /tmp on device via serial, execute it, read output file."""
import serial, time, sys

PORT = "COM7"
BAUD = 115200

# Script to run on device
SCRIPT = r"""#!/bin/sh
{
echo "=PKG="
dpkg -l modemmanager ppp isc-dhcp-client 2>/dev/null | awk '/^ii/{printf "%s %s\n",$2,$3}'
echo "=BIN="
ls /usr/sbin/pppd /usr/bin/mmcli 2>/dev/null || echo none
echo "=MM="
systemctl is-active ModemManager.service 2>/dev/null
echo "=APT="
apt-cache policy modemmanager 2>/dev/null | grep -E 'Installed|Candidate' | head -4
echo "=END="
} > /tmp/sa02m_modem_check.txt 2>&1
"""

def readall(s, timeout=3.0, idle=0.8):
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

def send_line(s, line, wait=0.5):
    s.write((line + "\n").encode())
    time.sleep(wait)
    return readall(s, wait + 0.5, 0.3)

s = serial.Serial(PORT, BAUD, timeout=0.2)
time.sleep(0.5); s.read(s.in_waiting)

# Ensure shell prompt
s.write(b"\n"); time.sleep(0.8); readall(s, 1.0, 0.3)

print("Writing script via heredoc...")
# Use heredoc to write the script
send_line(s, "cat > /tmp/chk.sh << 'EOFSCRIPT'", 0.3)
for line in SCRIPT.strip().split("\n"):
    send_line(s, line, 0.1)
send_line(s, "EOFSCRIPT", 0.5)
send_line(s, "chmod +x /tmp/chk.sh && sh /tmp/chk.sh", 1.0)
time.sleep(3)

# Read the output file
print("\nReading output...")
s.write(b"cat /tmp/sa02m_modem_check.txt\n")
out = readall(s, 5.0, 1.0)
print(out)

# If empty, try apt-get update + install directly and check
if "=PKG=" not in out:
    print("No output from file, trying direct check...")
    s.write(b"dpkg -l modemmanager 2>/dev/null | tail -3\n")
    print(readall(s, 4.0, 0.8))

s.close()
