"""Generate a saved config, actual bridge export and commissioning scan in a temp dir."""

import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "opt/sa02m-modbus-mqtt"))
import modbus_mqtt_bridge as bridge


INSTANCE = "1f" * 16


def device(kind, port, addr, **extras):
    return {"type": kind, "id": f"{kind}-COM{port}-{addr}", "port": f"/dev/COM{port}",
            "address": addr, **extras}


def setup(directory):
    scan = directory / "scan"
    scan.mkdir(exist_ok=True)
    runtime = directory / "bridge.json"
    config = directory / "config.yaml"
    devices = [device("mr02m", 4, 6, module_type=6), device("led", 4, 8),
               device("ce02m3", 2, 14), device("template", 4, 20, template="mtdx62-mb"),
               device("carel", 4, 1, family="crst"), device("mr02m", 5, 3, module_type=8)]
    pub = Mock()
    pub.device_online_snapshot.return_value = {d["id"]: True for d in devices}
    pub.device_online_snapshot.return_value.update({"led-COM4-8": False, "template-COM4-20": False})
    for did, state in pub.device_online_snapshot.return_value.items():
        bridge.DeviceLiveCache.set_online(did, state)
    with patch.dict(os.environ, INVOCATION_ID=INSTANCE), patch.object(bridge, "LIVE_CACHE_DIR", directory):
        bridge.write_bridge_roster(devices, pub, path=runtime)
    # Save without restarting. The id still contains COM5 after a move to COM4.
    devices[-1]["port"] = "/dev/COM4"
    devices.append(device("mr02m", 5, 22, module_type=8))
    config.write_text(json.dumps({"devices": devices}), encoding="utf-8")
    for port, found in {3: [(14, "CE-02m-3", True)], 4: [(71, "", False)],
                        5: [(3, "4DO6DI", True)]}.items():
        (scan / f"COM{port}.json").write_text(json.dumps({
            "ts": time.time() - 7200, "port": f"COM{port}",
            "devices": [{"addr": a, "model": m, "ours": ours} for a, m, ours in found]}))
    return config, runtime, scan


if __name__ == "__main__":
    setup(Path(sys.argv[1]))
