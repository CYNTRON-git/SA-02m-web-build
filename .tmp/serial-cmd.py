#!/usr/bin/env python3
"""Login to SA-02m via serial and run a command, returning output."""
import argparse
import re
import sys
import time

import serial


def read_until(s, patterns, timeout=10.0, echo=False):
    deadline = time.time() + timeout
    buf = bytearray()
    while time.time() < deadline:
        chunk = s.read(256)
        if chunk:
            buf.extend(chunk)
            if echo:
                sys.stderr.write(chunk.decode('utf-8', 'replace'))
                sys.stderr.flush()
            text = buf.decode('utf-8', 'replace')
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    return text, pat, m
    return buf.decode('utf-8', 'replace'), None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM7")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="cyntron")
    ap.add_argument("--cmd", required=True, help="command to run after login")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--echo", action="store_true")
    args = ap.parse_args()

    s = serial.Serial(args.port, args.baud, timeout=0.2)
    # Wake up, drain
    s.write(b"\r\n")
    time.sleep(0.3)
    s.reset_input_buffer()

    s.write(b"\r\n")
    text, pat, _ = read_until(s, [r"login:\s*$", r"\$\s*$", r"#\s*$"], timeout=3, echo=args.echo)

    if pat == r"login:\s*$":
        s.write((args.user + "\n").encode())
        text, pat, _ = read_until(s, [r"[Pp]assword:\s*$", r"#\s*$"], timeout=4, echo=args.echo)
        if pat == r"[Pp]assword:\s*$":
            s.write((args.password + "\n").encode())
            text, pat, _ = read_until(s, [r"#\s*$", r"\$\s*$", r"Login incorrect"], timeout=6, echo=args.echo)
            if pat is None or "Login incorrect" in text:
                print("LOGIN FAILED", file=sys.stderr)
                print(text, file=sys.stderr)
                sys.exit(2)

    # Now send command + a delimiter so we know where output ends.
    marker = "__SA02M_END_%d__" % int(time.time())
    full = f"{args.cmd}; echo {marker}\n"
    s.write(full.encode())
    text, pat, _ = read_until(s, [re.escape(marker)], timeout=args.timeout, echo=args.echo)
    # Strip everything up to first newline AFTER echo of command
    # Extract between command-echo and marker
    if marker in text:
        before, _, _ = text.rpartition(marker)
        # Drop the trailing prompt line if present
        lines = before.split("\n")
        out_lines = []
        seen_cmd = False
        for ln in lines:
            if not seen_cmd:
                if args.cmd[:30] in ln:
                    seen_cmd = True
                continue
            out_lines.append(ln)
        # Drop the final echo of marker command on a line by itself
        sys.stdout.write("\n".join(out_lines).rstrip() + "\n")
    else:
        sys.stdout.write(text)
    s.close()


if __name__ == "__main__":
    main()
