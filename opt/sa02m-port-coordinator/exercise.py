#!/usr/bin/env python3
"""Safe ownership cycles on configured UARTs; not a TCP gateway acceptance test."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

from sa02m_ports import Lease
from sa02m_ports.broker import occupants


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def unit_state(unit):
    result = subprocess.run(
        ["systemctl", "show", unit, "--property=ActiveState,UnitFileState"],
        capture_output=True, text=True, check=True, timeout=10)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def snapshot(ports, units):
    state = json.loads(Path("/run/sa02m-port-coordinator/status.json").read_text())
    return {
        "units": {unit: unit_state(unit) for unit in sorted(units)},
        "owners": {port: [
            {"pid": pid, "cgroup": Path("/proc/%d/cgroup" % pid).read_text().strip()}
            for pid in occupants(port)] for port in ports},
        "leases": state["leases"], "services": state["services"],
    }


def wait_restored(ports, units, baseline, timeout=30):
    until = time.monotonic() + timeout
    while True:
        current = snapshot(ports, units)
        if (current["units"] == baseline["units"] and not current["leases"]
                and not current["services"]
                and all({o["cgroup"] for o in baseline["owners"][p]} ==
                        {o["cgroup"] for o in current["owners"][p]} for p in ports)):
            return current
        if time.monotonic() >= until:
            raise RuntimeError("Baseline not restored: " + json.dumps(current))
        time.sleep(0.2)


def crc(data):
    value = 0xffff
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xa001 if value & 1 else 0)
    return value


def read_probe(port, baud, address):
    """FC04 input register zero, one word; never writes outputs or configuration."""
    import serial
    request = bytes([address, 4, 0, 0, 0, 1])
    request += crc(request).to_bytes(2, "little")
    with serial.Serial(port, baud, timeout=1, write_timeout=1, exclusive=True) as uart:
        uart.reset_input_buffer()
        uart.write(request)
        uart.flush()
        response = uart.read(7)
    if (len(response) != 7 or response[:3] != bytes([address, 4, 2])
            or crc(response[:-2]) != int.from_bytes(response[-2:], "little")):
        raise RuntimeError("Read-only Modbus probe failed: " + response.hex())
    return response.hex()


def client_death(port):
    child = subprocess.Popen(
        [sys.executable, __file__, "--child", port],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            if not selector.select(45):
                raise RuntimeError("Child lease did not become ready")
            if child.stdout.readline().strip() != "ready":
                raise RuntimeError("Child lease failed")
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ports", nargs="+", choices=["/dev/COM%d" % n for n in range(1, 6)])
    parser.add_argument("--cycles", type=int, default=100)
    parser.add_argument("--probe-address", type=int)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        with Lease(args.child, "scan"):
            fd = os.open(args.child, os.O_RDWR | os.O_NOCTTY)
            print("ready", flush=True)
            while True:
                time.sleep(10)
    if not args.ports or not 1 <= args.cycles <= 1000:
        parser.error("ports and 1..1000 cycles required")
    if len(set(args.ports)) != len(args.ports) or not 300 <= args.baud <= 4000000:
        parser.error("ports must be unique; baud must be 300..4000000")
    if args.probe_address is not None and not 1 <= args.probe_address <= 247:
        parser.error("probe address must be 1..247")
    config = json.loads(Path("/etc/sa02m/port-coordinator.json").read_text())
    units = {"sa02m-modbus-mqtt.service"}
    for port in args.ports:
        units.update(config["ports"][port])
    baseline = snapshot(args.ports, units)
    if baseline["leases"] or baseline["services"]:
        raise RuntimeError("Existing operation/restoration: refusing exercise")
    emit({"event": "baseline", "physical_exchange": args.probe_address is not None,
          "kind": "UART ownership; gateway purpose is not TCP mode verification",
          "state": baseline})
    for number in range(args.cycles):
        start = time.monotonic()
        port = args.ports[number % len(args.ports)]
        scenario = number % 5
        response = None
        try:
            if scenario == 4:
                client_death(port)
            else:
                with ExitStack() as stack:
                    stack.enter_context(Lease(port, "gateway" if scenario == 1 else "scan"))
                    if scenario == 2 and len(args.ports) > 1:
                        second = args.ports[(number + 1) % len(args.ports)]
                        stack.enter_context(Lease(second, "scan"))
                    if args.probe_address is not None:
                        response = read_probe(port, args.baud, args.probe_address)
                    else:
                        fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
                        os.close(fd)
                    if scenario == 3:
                        raise InterruptedError("injected safe cancellation")
        except InterruptedError:
            pass
        restored = wait_restored(args.ports, units, baseline)
        emit({"event": "cycle", "cycle": number + 1, "scenario": scenario,
              "port": port, "duration_s": round(time.monotonic() - start, 3),
              "probe": response, "state": restored})
    emit({"event": "complete", "cycles": args.cycles})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit({"event": "failed", "error": str(exc)})
        raise SystemExit(1)
