#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply 12AI COM4 Short-response fix on the SA-02m where this script runs.

Usage on device:
  python3 fix_12ai_com4_on_device.py
  # or with a bridge source to install:
  python3 fix_12ai_com4_on_device.py /tmp/modbus_mqtt_bridge.py

What it does:
  * backup bridge + /etc/sa02m-modbus-mqtt.yaml
  * set MR02M_AI_READ_CHUNK_REGS = 21 (via install of source, or in-place sed)
  * set poll_ai_ao_s: 2 (and poll_s: 2 if <2) for device id mr02m-COM4-12
  * py_compile + systemctl restart sa02m-modbus-mqtt
  * print roster / recent journal hints
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
import sys
from pathlib import Path

BRIDGE = Path("/opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py")
YAML = Path("/etc/sa02m-modbus-mqtt.yaml")
STAMP = dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(p: Path) -> Path:
    bak = Path(f"{p}.bak-{STAMP}")
    shutil.copy2(p, bak)
    print(f"backup: {bak}")
    return bak


def install_bridge(src: Path | None) -> None:
    backup(BRIDGE)
    if src is not None:
        shutil.copy2(src, BRIDGE)
        BRIDGE.chmod(0o755)
        text = BRIDGE.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        BRIDGE.write_text(text, encoding="utf-8")
    else:
        text = BRIDGE.read_text(encoding="utf-8")
        new, n = re.subn(
            r"^MR02M_AI_READ_CHUNK_REGS\s*=\s*42\s*$",
            "MR02M_AI_READ_CHUNK_REGS = 21",
            text,
            count=1,
            flags=re.M,
        )
        if n == 0 and "MR02M_AI_READ_CHUNK_REGS = 21" not in text:
            raise SystemExit("could not find MR02M_AI_READ_CHUNK_REGS = 42 to patch")
        if n:
            BRIDGE.write_text(new, encoding="utf-8")
    m = re.search(r"^MR02M_AI_READ_CHUNK_REGS\s*=\s*(\d+)", BRIDGE.read_text(encoding="utf-8"), re.M)
    print(f"bridge chunk = {m.group(1) if m else '?'}")
    subprocess.check_call([sys.executable, "-m", "py_compile", str(BRIDGE)])


def patch_yaml() -> None:
    backup(YAML)
    text = YAML.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_dev = False
    dev_indent: int | None = None
    patched_ai = False
    patched_poll_s = False

    for line in lines:
        m_id = re.match(r"^(\s*)-\s+id:\s*[\"']?(mr02m-COM4-12)[\"']?\s*$", line)
        m_any = re.match(r"^(\s*)-\s+id:\s*", line)
        if m_id:
            in_dev = True
            dev_indent = len(m_id.group(1))
            out.append(line)
            continue
        if in_dev and m_any and len(m_any.group(1)) <= (dev_indent or 0):
            in_dev = False
        if in_dev:
            m_ai = re.match(r"^(\s*)poll_ai_ao_s:\s*([0-9.]+)\s*$", line)
            m_ps = re.match(r"^(\s*)poll_s:\s*([0-9.]+)\s*$", line)
            if m_ai:
                out.append(f"{m_ai.group(1)}poll_ai_ao_s: 2\n")
                patched_ai = True
                continue
            if m_ps and float(m_ps.group(2)) < 2:
                out.append(f"{m_ps.group(1)}poll_s: 2\n")
                patched_poll_s = True
                continue
        out.append(line)

    if not patched_ai:
        out2: list[str] = []
        for line in out:
            out2.append(line)
            if re.match(r"^\s*-\s+id:\s*[\"']?mr02m-COM4-12", line):
                ind = re.match(r"^(\s*)-", line).group(1) + "  "
                out2.append(f"{ind}poll_ai_ao_s: 2\n")
                patched_ai = True
        out = out2

    new = "".join(out)
    if new != text:
        YAML.write_text(new, encoding="utf-8")
    print(f"yaml patched_ai={patched_ai} patched_poll_s={patched_poll_s}")
    if not patched_ai:
        raise SystemExit("device mr02m-COM4-12 not found in YAML")


def restart_and_report() -> None:
    subprocess.check_call(["systemctl", "restart", "sa02m-modbus-mqtt"])
    subprocess.check_call(["sleep", "2"])
    st = subprocess.check_output(["systemctl", "is-active", "sa02m-modbus-mqtt"], text=True).strip()
    print(f"service: {st}")
    # journal snippet
    jc = subprocess.run(
        ["journalctl", "-u", "sa02m-modbus-mqtt", "-n", "40", "--no-pager"],
        capture_output=True, text=True,
    )
    for line in jc.stdout.splitlines():
        if re.search(r"COM4-12|Short response|offline|chunk|AI block", line, re.I):
            print("journal:", line)
    roster = Path("/run/sa02m-modbus-mqtt/_roster.json")
    if roster.is_file():
        print("roster:", roster.read_text(encoding="utf-8")[:2000])


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if src is not None and not src.is_file():
        raise SystemExit(f"bridge source not found: {src}")
    if not BRIDGE.is_file() or not YAML.is_file():
        raise SystemExit("not an SA-02m host (bridge/yaml missing)")
    install_bridge(src)
    patch_yaml()
    restart_and_report()
    print("OK — watch 1–2 min: journalctl -u sa02m-modbus-mqtt -f")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
