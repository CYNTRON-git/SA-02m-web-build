"""Qualify a provisioned legacy client and a prepared managed gateway in loopback."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def main():
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("Use a dedicated loopback network namespace")
    old = Path(os.environ["SA02M_OPCUA_CLIENT_PYTHON"])
    new = Path(os.environ["SA02M_OPCUA_MANAGED_PYTHON"])
    active = Path(os.environ["SA02M_OPCUA_MANAGED_ENTRY"])
    command = json.loads(os.environ["SA02M_OPCUA_MANAGED_COMMAND"])
    output = Path(os.environ["SA02M_OPCUA_REPORT_DIR"])
    if not all(p.is_absolute() for p in (old, new, active, output)):
        raise ValueError("All fixture paths must be absolute")
    if not isinstance(command, list) or not command or not all(isinstance(s, str) for s in command):
        raise ValueError("SA02M_OPCUA_MANAGED_COMMAND must be an argv array")
    if not Path(command[0]).is_absolute():
        raise ValueError("Supply an absolute fixed launcher path")
    entry = HERE.parent / "sa02m-mqtt-opcua-managed.py"
    if active.read_bytes() != entry.read_bytes():
        raise ValueError("Managed entry differs from the source under qualification")
    lock = Path(os.environ["SA02M_OPCUA_LOCK"])
    pins = {line.split("==")[0]: line.split("==")[1].split()[0] for line in lock.read_text().splitlines()}
    if pins.get("asyncua") != "2.1.0" or pins.get("paho-mqtt") != "2.1.0":
        raise ValueError("Unsupported gateway runtime lock")
    output.mkdir(parents=True, exist_ok=False)
    origin_check = """
import importlib, importlib.metadata as md, json, pathlib, sys, sysconfig
site = pathlib.Path(sysconfig.get_path('purelib')).resolve()
assert sys.flags.isolated and sys.prefix != sys.base_prefix
pins, names = json.loads(sys.argv[1:][0])
assert all(md.version(k) == v for k, v in pins.items()), pins
for key in pins:
    assert pathlib.Path(md.distribution(key).locate_file('')).resolve() == site, key
origins = {n: str(pathlib.Path(importlib.import_module(n).__file__).resolve()) for n in names}
assert all(pathlib.Path(p).is_relative_to(site) for p in origins.values()), origins
print(json.dumps(dict(executable=sys.executable, prefix=sys.prefix, pins=pins, origins=origins)))
"""
    for label, python, versions, imports in (
        ("old", old, {"opcua": "0.98.13", "paho-mqtt": "2.1.0"}, ["opcua", "paho.mqtt.client"]),
        ("managed", new, pins, ["asyncua.sync", "_cffi_backend", "cryptography.hazmat.bindings._rust", "paho.mqtt.client"]),
    ):
        result = subprocess.check_output([python, "-I", "-B", "-c", origin_check, json.dumps([versions, imports])])
        (output / (label + "-origins.json")).write_bytes(result)
    subprocess.run([new, "-I", "-B", HERE / "managed_transport_fixture.py"], timeout=240, check=True)
    for mode, target in (("legacy", [old, "-I", "-B", HERE.parent / "sa02m-mqtt-opcua.py"]), ("managed", command)):
        strict = ["--strict-writes"] if mode == "managed" else []
        subprocess.run([old, "-I", "-B", HERE / "protocol_fixture.py", *strict, output / mode, *target], timeout=240, check=True)
    old_nodes = json.loads((output / "legacy/result.json").read_bytes())["nodes"]
    new_nodes = json.loads((output / "managed/result.json").read_bytes())["nodes"]
    if new_nodes != old_nodes:
        raise ValueError("Old and new NodeIds, types or values differ")
    print(json.dumps({"entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
                      "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(), "nodes": new_nodes}))


if __name__ == "__main__":
    main()
