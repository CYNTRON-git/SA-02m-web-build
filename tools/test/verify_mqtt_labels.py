#!/usr/bin/env python3
"""Verify MQTT bridge vs broker labels and status on device."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_common import WEB_COOKIE, ssh_run  # noqa: E402


def main() -> None:
    print("=== SERVICE_DEFS labels ===")
    print(ssh_run("grep -E '^(mosquitto|mqtt-)' /usr/local/sbin/sa02m-web-service-ctl.sh | head -5"))

    print("\n=== ctl list (mqtt*) ===")
    raw = ssh_run("sudo -n /usr/local/sbin/sa02m-web-service-ctl.sh list")
    data = json.loads(raw.splitlines()[-1])
    for s in data.get("services", []):
        if s.get("id") in ("mosquitto", "mqtt-bridge", "mqtt-telemetry"):
            print(
                s["id"],
                "label=", s.get("label"),
                "active=", s.get("active"),
                "user_disabled=", s.get("user_disabled"),
            )

    print("\n=== status.cgi services part ===")
    raw2 = ssh_run(
        f"curl -s -b '{WEB_COOKIE}' 'http://127.0.0.1:9999/cgi-bin/status.cgi?part=services'"
    )
    st = json.loads(raw2.splitlines()[-1])
    print("svc_mosquitto:", st.get("svc_mosquitto"))
    print("svc_bridge:", st.get("svc_bridge"))

    print("\n=== mqtt_status.cgi ===")
    raw3 = ssh_run(f"curl -s -b '{WEB_COOKIE}' 'http://127.0.0.1:9999/cgi-bin/mqtt_status.cgi'")
    mq = json.loads(raw3.splitlines()[-1])
    print(
        "mosquitto_active=", mq.get("mosquitto_active"),
        "mosquitto_disabled=", mq.get("mosquitto_disabled"),
        "bridge_active=", mq.get("bridge_active"),
        "bridge_disabled=", mq.get("bridge_disabled"),
    )

    bridge = next(s for s in data["services"] if s["id"] == "mqtt-bridge")
    mosq = next(s for s in data["services"] if s["id"] == "mosquitto")
    assert bridge.get("label") == "MQTT мост", f"bad bridge label: {bridge.get('label')!r}"
    assert st.get("svc_bridge") in ("active", "inactive", "disabled"), "bad svc_bridge"
    assert st.get("svc_mosquitto") in ("active", "inactive", "disabled"), "bad svc_mosquitto"
    if bridge.get("user_disabled"):
        assert st.get("svc_bridge") == "disabled", "bridge disabled mismatch"
        assert mq.get("bridge_disabled") == 1, "mqtt_status bridge_disabled mismatch"
    if mosq.get("user_disabled"):
        assert st.get("svc_mosquitto") == "disabled"
    else:
        if mq.get("mosquitto_active") == 1:
            assert st.get("svc_mosquitto") == "active", "mosquitto active mismatch"
    print("\nOK: labels and status consistency verified")


if __name__ == "__main__":
    main()
