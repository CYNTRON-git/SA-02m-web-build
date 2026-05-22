#!/usr/bin/env python3
import sys, time, serial
PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"
ser = serial.Serial(PORT, 115200, timeout=0.2)
ser.dtr = True
ser.rts = False
time.sleep(0.3)
cmds = [
    "hostname",
    "ip -4 addr show",
    "systemctl is-active ssh",
    "ls /etc/ssh/ssh_host_* 2>&1 | head -5",
    "test -f /root/.not_logged_in_yet && echo FIRSTRUN_SET || echo FIRSTRUN_CLEAR",
    "test -f /etc/rc.local && cat /etc/rc.local || echo NO_RCLOCAL",
    "systemctl is-enabled regen-ssh-host-keys.service 2>&1 || true",
    "df -h /",
]
for c in cmds:
    ser.write((c + "\r\n").encode())
    time.sleep(1.5)
    buf = b""
    while ser.in_waiting:
        buf += ser.read(ser.in_waiting)
        time.sleep(0.05)
    print(f">>> {c}")
    print(buf.decode("utf-8", errors="replace"))
ser.close()
