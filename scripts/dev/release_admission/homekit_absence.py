"""Observe the optional HomeKit installation and effective unit on a Linux release target."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


UNIT = "sa02m-homekit.service"
PATHS = ("/opt/sa02m-homekit", "/opt/sa02m-homekit-venv", "/opt/sa02m-homekit-venv/bin/python")
ABSENT_UNIT = {"Id": UNIT, "LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead",
               "MainPID": "0", "ControlPID": "0", "FragmentPath": "", "DropInPaths": "", "ExecStart": ""}
DPKG_FORMAT = "${db:Status-Status}\t${binary:Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\n"


def observe():
    raw = subprocess.run(["/usr/bin/systemctl", "show", UNIT, "--all", "--property=" + ",".join(ABSENT_UNIT)],
                         capture_output=True, text=True, timeout=15)
    unit = dict(line.split("=", 1) for line in raw.stdout.splitlines() if line)
    if unit.get("LoadState") == "not-found" and "ExecStart" not in unit:
        # systemd omits an empty command array even with --all on Noble.
        unit["ExecStart"] = subprocess.check_output(["/usr/bin/systemctl", "show", UNIT, "--value", "--property=ExecStart"],
                                                     text=True, timeout=15).strip()
    if raw.returncode not in (0, 4) or unit.keys() != ABSENT_UNIT.keys():
        raise ValueError("effective HomeKit unit unavailable")
    processes = []
    for proc in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = proc.read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            continue
        if any(b"/opt/sa02m-homekit" in arg or b"sa02m_homekit" in arg for arg in args):
            processes.append(int(proc.parent.name))
    dpkg = subprocess.check_output(["/usr/bin/dpkg-query", "-W", "-f=" + DPKG_FORMAT], timeout=45)
    return {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "kernel_release": os.uname().release,
            "dpkg_sha256": hashlib.sha256(dpkg).hexdigest(), "paths": {p: os.path.lexists(p) for p in PATHS},
            "unit": unit, "processes": sorted(processes)}


def matches(receipt, board, target, now):
    if not isinstance(receipt, dict) or type(receipt.get("captured")) not in (int, float):
        return False
    boot = board.get("boot_id")
    return (receipt.get("schema") == 1 and receipt.get("target") == target and 0 <= now - receipt["captured"] <= 86400
            and isinstance(boot, str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot) is not None
            and receipt.get("boot_id") == boot and receipt.get("kernel_release") == board["kernel_release"]
            and receipt.get("dpkg_sha256") == hashlib.sha256(board["dpkg"]["stdout"].encode()).hexdigest()
            and board["homekit"].get("exists") is False and board["homekit"].get("distributions") == []
            and isinstance(receipt.get("paths"), dict) and receipt["paths"].keys() == set(PATHS)
            and all(value is False for value in receipt["paths"].values())
            and receipt.get("unit") == ABSENT_UNIT and receipt.get("processes") == [])


if __name__ == "__main__":
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", sys.argv[1]) or os.geteuid() != 0:
        raise SystemExit("Usage on target as root: python3 -I -B homekit_absence.py TARGET")
    started = time.time()
    observed = observe()
    if observed != observe():
        raise SystemExit("HomeKit evidence changed during collection")
    print(json.dumps({"schema": 1, "target": sys.argv[1], "captured": started, **observed}, sort_keys=True))
