#!/usr/bin/env python3
"""Verify MQTT status fix: disabled-but-running and stop/start cycle."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_common import WEB_COOKIE, ssh_run  # noqa: E402


def fetch(url):
    return json.loads(ssh_run(f"curl -s -b '{WEB_COOKIE}' '{url}'"))


def svc_bridge():
    svc = fetch("http://127.0.0.1:9999/cgi-bin/services_ctrl.cgi")
    return next(s for s in svc["services"] if s["id"] == "mqtt-bridge")


def ctl_state(svc):
    if svc.get("user_disabled") or svc.get("masked"):
        return "disabled"
    return "active" if svc.get("active") == "active" else "inactive"


def mqtt_state(mqtt):
    if mqtt.get("bridge_disabled"):
        return "disabled"
    return "active" if mqtt.get("bridge_active") else "inactive"


def compare(label):
    svc = svc_bridge()
    mqtt = fetch("http://127.0.0.1:9999/cgi-bin/mqtt_status.cgi")
    cs = ctl_state(svc)
    ms = mqtt_state(mqtt)
    ok = cs == ms
    print(
        f"{label}: ctl={cs} mqtt={ms} bridge_active={mqtt.get('bridge_active')} "
        f"bridge_disabled={mqtt.get('bridge_disabled')} -> {'OK' if ok else 'MISMATCH'}"
    )
    return ok


def main():
    ssh_run("sudo /usr/local/sbin/sa02m-web-service-ctl.sh start mqtt-bridge")
    time.sleep(4)
    if not compare("running"):
        return 1

    ssh_run("sudo systemctl disable sa02m-modbus-mqtt.service")
    time.sleep(1)
    if not compare("disabled+running proc"):
        return 1

    ssh_run("sudo /usr/local/sbin/sa02m-web-service-ctl.sh stop mqtt-bridge")
    time.sleep(6)
    if not compare("stopped via ctl"):
        return 1

    ssh_run(
        f"curl -s -b '{WEB_COOKIE}' -X POST -H 'Content-Type: application/json' "
        "-d '{\"id\":\"mqtt-bridge\",\"action\":\"start\"}' "
        "'http://127.0.0.1:9999/cgi-bin/services_ctrl.cgi'"
    )
    time.sleep(8)
    if not compare("started via services"):
        return 1

    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
