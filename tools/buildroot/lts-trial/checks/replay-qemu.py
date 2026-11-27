#!/usr/bin/env python3
"""Repeat a qualified air-gapped QEMU boot with the recorded inputs."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("smp", "rt"))
    parser.add_argument("kernel", type=Path)
    parser.add_argument("dtb", type=Path)
    parser.add_argument("initrd", type=Path)
    parser.add_argument("log", type=Path)
    args = parser.parse_args()
    receipt = Path(__file__).with_name(args.profile + "-qemu.txt").read_text().splitlines()
    for record, path in zip(receipt[:3], (args.kernel, args.dtb, args.initrd)):
        if hashlib.sha256(path.read_bytes()).hexdigest() != record.split()[1]:
            parser.error(f"Unqualified input: {path}")
    # Supply the entropy normally handed off by a bootloader. The emulated H3
    # has no external entropy device; wg genkey waits for the guest CRNG.
    with tempfile.TemporaryDirectory(prefix="sa02m-qemu-") as directory:
        overlay = Path(directory) / "seed.dtbo"
        boot_dtb = Path(directory) / "boot.dtb"
        seed = os.urandom(32).hex(" ")
        dts = f'/dts-v1/; /plugin/; &{{/chosen}} {{ rng-seed = [{seed}]; }};'
        subprocess.run(["dtc", "-I", "dts", "-O", "dtb", "-o", str(overlay), "-"],
                       input=dts, text=True, check=True)
        subprocess.run(["fdtoverlay", "-i", str(args.dtb), "-o", str(boot_dtb), str(overlay)],
                       check=True)
        cmd = ["qemu-system-arm", "-machine", "orangepi-pc", "-smp", "4", "-m", "1024M",
               "-nic", "none", "-display", "none", "-monitor", "none", "-serial", "stdio",
               "-no-reboot", "-kernel", str(args.kernel), "-dtb", str(boot_dtb), "-initrd",
               str(args.initrd), "-append", "console=ttyS0,115200 earlycon rdinit=/init panic=-1"]
        with args.log.open("xb") as log:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    time.sleep(.5)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=10)
    marker = next(line for line in receipt if line.startswith("VPN_BOOT_TEST_OK"))
    data = args.log.read_text(errors="replace")
    required = [marker, "tun type tun", "#0: H3 Audio Codec", "VPN_BOOT peer=a", "VPN_BOOT peer=b"]
    if args.profile == "rt":
        required.append("VPN_BOOT PREEMPT_RT=1")
    if (not all(w in data for w in required)
            or data.count("2 packets transmitted, 2 packets received, 0% packet loss") != 4
            or any(w in data for w in ("Internal error: Oops", "Kernel panic", "BUG:", "WARNING: CPU"))):
        raise SystemExit(f"QEMU acceptance failed; see {args.log}")
    print(marker)


if __name__ == "__main__":
    main()
