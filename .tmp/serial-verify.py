#!/usr/bin/env python3
import serial, time, sys

PORT = "COM7"; BAUD = 115200

def waitprompt(s, timeout=15):
    buf = b""; t = time.time() + timeout
    while time.time() < t:
        c = s.read(s.in_waiting or 1)
        if c:
            buf += c
            sys.stdout.write(c.decode(errors="replace")); sys.stdout.flush()
            if b"# " in buf[-10:]: return buf
        else: time.sleep(0.05)
    return buf

def run(s, cmd, timeout=20):
    s.write((cmd + "\n").encode()); return waitprompt(s, timeout)

s = serial.Serial(PORT, BAUD, timeout=0.2)
time.sleep(0.5); s.read(s.in_waiting)
s.write(b"\n"); waitprompt(s, 5)

run(s, "dpkg -l modemmanager ppp isc-dhcp-client | awk '/^[ih]/{printf \"%-20s %-30s %s\\n\",$2,$3,$1}'")
run(s, "which mmcli pppd dhclient 2>/dev/null")
run(s, "systemctl is-active ModemManager.service && echo MM-ACTIVE || echo MM-INACTIVE")
run(s, "ping -c1 -W2 8.8.8.8 >/dev/null && echo INET-OK || echo INET-FAIL")
s.close()
