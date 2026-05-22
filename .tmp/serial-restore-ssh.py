#!/usr/bin/env python3
import sys
import time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"
BAUD = 115200


def drain(ser, t=0.5):
    time.sleep(t)
    buf = b""
    while ser.in_waiting:
        buf += ser.read(ser.in_waiting)
        time.sleep(0.03)
    return buf


def send_line(ser, text, wait=1.0):
    ser.write((text + "\r\n").encode())
    sys.stdout.buffer.write(f">>> {text}\n".encode())
    out = drain(ser, wait).decode("utf-8", errors="replace")
    sys.stdout.buffer.write(out.encode("utf-8", errors="replace"))
    return out


def main():
    ser = serial.Serial(PORT, BAUD, timeout=0.2)
    ser.dtr = True
    ser.rts = False
    time.sleep(0.2)
    send_line(ser, "", 0.3)

    out = send_line(ser, "ssh-keygen -A", 15.0)
    out = send_line(ser, "systemctl enable ssh", 2.0)
    out = send_line(ser, "systemctl restart ssh", 3.0)
    out = send_line(ser, "ss -tlnp | grep :22 || true", 2.0)
    out = send_line(ser, "systemctl is-active ssh", 2.0)
    out = send_line(ser, "rm -f /root/.not_logged_in_yet", 1.0)
    out = send_line(ser, "echo RESTORE_OK", 1.0)

    ser.close()
    ok = "RESTORE_OK" in out and ("active" in out or ":22" in out)
    sys.stdout.buffer.write(f"\nOK={ok}\n".encode())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
