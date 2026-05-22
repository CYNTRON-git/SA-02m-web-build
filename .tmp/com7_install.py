"""Send install commands to SA-02m via COM7 serial port."""
import serial
import time
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

PORT = "COM7"
BAUD = 115200

COMMANDS = [
    ("DNS fix", "printf 'nameserver 8.8.8.8\\nnameserver 8.8.4.4\\n' > /etc/resolv.conf && echo DNS_OK", 5),
    ("apt update", "DEBIAN_FRONTEND=noninteractive apt-get update -q 2>&1 | tail -5", 120),
    ("apt install", "DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard-tools nginx fcgiwrap spawn-fcgi libarchive-tools 2>&1 | tail -10", 300),
    ("nginx enable", "systemctl enable nginx fcgiwrap && echo ENABLED", 5),
    ("check services", "systemctl is-active nginx fcgiwrap; ss -tlnp | grep -E ':80|:9999'", 5),
    ("wireguard check", "wg --version && modinfo wireguard 2>/dev/null | head -2 || echo WG_BUILTIN", 5),
    ("ping test", "ping -c 1 -W 3 8.8.8.8 2>&1 | tail -1", 8),
]


def run_cmd(s, cmd, wait=10):
    s.read(s.in_waiting or 1)
    s.write((cmd + "\r\n").encode())
    out = b""
    t = time.time()
    deadline = t + wait + 3
    while time.time() < deadline:
        chunk = s.read(4096)
        if chunk:
            out += chunk
            deadline = time.time() + 3  # extend on activity
        if b"~#" in out[-50:] or b"$ " in out[-10:]:
            break
        time.sleep(0.1)
    text = re.sub(rb"\x1b\[[0-9;]*[mhJKH]|\x1b\[\?[0-9]+[hl]", b"", out).decode(errors="replace")
    lines = text.split("\r\n")
    return "\r\n".join(lines[1:]).strip()


def main():
    print(f"Opening {PORT}...")
    s = serial.Serial(PORT, BAUD, timeout=2)
    time.sleep(0.3)
    s.read(4096)
    s.write(b"\r\n")
    time.sleep(0.5)
    s.read(4096)
    print("Connected to SA-02m shell\n")

    for label, cmd, wait in COMMANDS:
        print(f">>> [{label}]")
        print(f"    cmd: {cmd[:60]}...")
        result = run_cmd(s, cmd, wait)
        for line in result.split("\r\n"):
            line = line.strip()
            if line and "root@SA-02" not in line:
                print(f"    {line}")
        print()

    s.close()
    print("Done.")


if __name__ == "__main__":
    main()
