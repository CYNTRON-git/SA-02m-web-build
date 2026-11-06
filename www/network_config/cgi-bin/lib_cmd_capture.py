#!/usr/bin/env python3
"""Collect a bounded console tail without waiting indefinitely for pipe EOF."""
import json
import os
import selectors
import sys
import time

TAIL_BYTES = 32768
READ_BYTES = 65536
CAPTURE_SECONDS = 18


def capture(fd):
    tail = b""
    observed = 0
    timed_out = False
    deadline = time.monotonic() + CAPTURE_SECONDS
    try:
        os.set_blocking(fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if not selector.select(remaining):
                    continue
                try:
                    chunk = os.read(fd, READ_BYTES)
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                observed += len(chunk)
                tail = (tail + chunk)[-TAIL_BYTES:]
    finally:
        os.close(fd)
    return tail, {"observed_bytes": observed, "capture_timed_out": timed_out}


def main():
    if len(sys.argv) != 3:
        return 64
    try:
        tail, metadata = capture(sys.stdin.fileno())
        with open(sys.argv[1], "wb") as output:
            output.write(tail)
        with open(sys.argv[2], "w", encoding="ascii") as output:
            json.dump(metadata, output)
    except (OSError, ValueError):
        print("console capture failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
