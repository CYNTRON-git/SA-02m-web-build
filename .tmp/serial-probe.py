#!/usr/bin/env python3
"""Probe COM7 to see what the device is doing right now."""
import argparse
import sys
import time

try:
    import serial
except ImportError:
    print("Installing pyserial...", file=sys.stderr)
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyserial"], stdout=sys.stderr)
    import serial

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM7")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=15.0, help="how long to listen")
    ap.add_argument("--send", help="optional newline-terminated command to send first")
    args = ap.parse_args()

    print(f"[serial] opening {args.port} @ {args.baud}", file=sys.stderr)
    try:
        s = serial.Serial(args.port, args.baud, timeout=0.2)
    except Exception as exc:
        print(f"[serial] open failed: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.send:
        cmd = (args.send + "\n").encode("utf-8", "replace")
        print(f"[serial] sending: {args.send!r}", file=sys.stderr)
        s.write(b"\r\n")  # wake-up newline
        time.sleep(0.2)
        s.write(cmd)

    t0 = time.time()
    buf = bytearray()
    last_print = t0
    while time.time() - t0 < args.seconds:
        chunk = s.read(256)
        if chunk:
            buf.extend(chunk)
        if time.time() - last_print > 0.5 and buf:
            try:
                txt = buf.decode("utf-8", "replace")
            except Exception:
                txt = repr(bytes(buf))
            sys.stdout.write(txt)
            sys.stdout.flush()
            buf.clear()
            last_print = time.time()
    if buf:
        sys.stdout.write(buf.decode("utf-8", "replace"))
    s.close()
    print(f"\n[serial] done ({args.seconds}s)", file=sys.stderr)

if __name__ == "__main__":
    main()
